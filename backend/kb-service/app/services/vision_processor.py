"""
Vision 处理服务 - KBD 图片语义化（Vision LLM）

功能：
  从 kbd_image 表读取原始图片，调用 Vision LLM 生成图片描述，
  更新 kbd_entry.images_json 并重建 content_md。

核心流程：
  Step 0  解析文档 HTML，提取每张图片的上下文文字（纯 Python，无 LLM）
  Step 1  Vision LLM 单次调用（图片 + context + Prompt）
            -> TYPE（截图类型：终端/日志/告警/任务/配置/其他）
            -> BACKGROUND（背景颜色）
            -> FULL_TEXT（文字原文照录）
            -> DESCRIPTION（语义描述）
  Step 2  组装 images_json，更新 kbd_entry，重建 content_md

设计原则：
  - Prompt 从 system_prompt 表热加载（admin-ui 修改后立即生效）
  - 图片从 kbd_image 表读取（解耦 data-pipeline 本地文件系统）
  - 单次 LLM 调用，效率最大化
  - 并发控制：全局共享 Semaphore 限制同时调用的 Vision LLM 数量

P0 死锁根因与修复（重要）：
  旧实现：VISION 在「持有 DB session / 连接」的协程内，直接 await 一个长达 ~60s 的
  Vision LLM 同步调用（阻塞事件循环），且多案例串行。后果：
    1) 长耗时 await 占住 asyncpg 连接不释放，连接池被长期占用；
    2) 多案例并发时连接池耗尽，后续 DB 操作排队 → 互相等待 → 死锁/超时（300s 后失败）；
    3) 请求线程长时间挂起，网关 300s 超时。
  本实现的三处关键修复：
    A) Asynchronous Request-Reply：POST 立即返回 202 + job_id，识图在后台 task 执行，
       不阻塞 HTTP 请求（见 vision_job_manager）；
    B) 释放 DB 连接：先在一个 short-lived session 内「只读」取数据，全部拉入内存后再
       释放连接，Vision 调用期间「不持有任何 DB 连接」；写回时再开一个 short-lived session；
    C) 全局共享信号量收敛 LLM 并发（见 _get_vision_semaphore），避免打爆 DashScope QPM。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import os
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from openai import APIConnectionError, APITimeoutError, AsyncOpenAI
from shared.observability.langfuse import observe_llm_generation, update_observation
from shared.observability.logger import get_logger
from shared.observability.metrics import KBD_LLM_REQUESTS_TOTAL, KBD_LLM_TOKENS_TOTAL
from shared.observability.otel import get_current_trace_id
from shared.utils.prompt_loader import StrictPromptLoader
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.kbd_entry import KbdEntry, KbdImage
from app.services.kbd_mutation_guard import require_mutable_kbd
from app.services.kbd_revision_service import freeze_kbd_ai_proposal
from app.services.llm_runtime import get_llm_semaphore

logger = get_logger("kb-service-vision-processor")


class VisionEmptyResultError(RuntimeError):
    """Vision 模型连续返回无法形成证据的空结果。"""


# ──────────────────────────────────────────────────────────────────────────────
# 常量配置
# ──────────────────────────────────────────────────────────────────────────────

_MIN_CONTEXT_CHARS = 80
_SHORT_WINDOW = 300
_LONG_WINDOW = 800
_MAX_VISION_IMAGE_SIZE = 2 * 1024 * 1024  # 文字截图优先保真；仅超 2MB 才缩放/压缩
_VISION_CONCURRENCY = 3  # 并发 LLM 调用数（P1-1 异步化后提高，配合异步 Job 批量跑；DashScope QPM 内安全）

# 截图类型与背景颜色（LLM 输出标准）
_SCREENSHOT_TYPES = ("终端截图", "日志截图", "告警截图", "任务截图", "弹框截图", "配置截图", "其他截图")
_BACKGROUND_COLORS = ("白色", "黑色", "灰色", "彩色", "其他")

# 任务详情在 HCI 中经常以模态窗口承载。容器外观只是展示方式，不能覆盖窗口内
# 已明确可见的任务记录语义；这些字段组合才是可复核的分类依据。
_TASK_DETAIL_REQUIRED_FIELDS = ("状态", "行为")
_TASK_DETAIL_SUPPORTING_FIELDS = ("起始时间", "开始时间", "结束时间", "完成时间", "对象类型", "对象", "主机", "用户名")

# Vision Prompt 名称（从 system_prompt 表热加载）
_KBD_VISION_PROMPT_NAME = "kbd_vision_v1"

# LLM 配置（从环境变量读取，与 classify.py 保持一致，统一使用 LLM_* 命名）
# 模型来源：VISION_MODEL 由 Helm ConfigMap(hci-common-config) 注入，取自 kbService.visionModel，
# 未配置时回退到 kimi-k2.5。所有 LLM 相关开关（LLM_TIMEOUT / LLM_ENABLE_THINKING /
# VISION_GLOBAL_CONCURRENCY）均来自同一 ConfigMap，确保 data-pipeline→kb-service→LLM 链路统一。
_LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "").rstrip("/")
_LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
# 优先读取 LLM_VISION_* 环境变量以支持专用 Vision 端点，否则回退到通用 LLM 配置
_LLM_VISION_BASE_URL = (os.environ.get("LLM_VISION_BASE_URL") or os.environ.get("LLM_BASE_URL", "")).rstrip("/")
_LLM_VISION_API_KEY = os.environ.get("LLM_VISION_API_KEY") or os.environ.get("LLM_API_KEY", "")
# 优先读取 VISION_MODEL，若未配置，则回退到已验证可用的 kimi-k2.5（支持多模态识图）
_LLM_VISION_MODEL = os.environ.get("VISION_MODEL", "kimi-k2.5")
# Vision 专用超时（秒），未配置时回退到通用 LLM_TIMEOUT
_LLM_VISION_TIMEOUT = float(os.environ.get("LLM_VISION_TIMEOUT") or os.environ.get("LLM_TIMEOUT", "120.0"))
_LLM_TIMEOUT = float(os.environ.get("LLM_TIMEOUT", "120.0"))
_VISION_MAX_TOKENS = int(os.environ.get("VISION_MAX_TOKENS", "1536"))
# 是否启用 LLM 思维链（thinking）。默认关闭：kimi-k2.5 / glm-5 等模型开启 thinking 时
# 会在正式回答前生成大量隐藏思考 token，使 Vision 调用延迟飙升并突破 LLM_TIMEOUT。
# 统一由 LLM_ENABLE_THINKING 环境变量开关控制（与 classify.py 保持一致，单一真相源）。
_LLM_ENABLE_THINKING = os.environ.get("LLM_ENABLE_THINKING", "false").lower() in ("1", "true", "yes", "on")

# 全局共享信号量：收敛所有并发 KBD 任务的 Vision LLM 调用总数。
# P0-6 修复：旧实现每个 kbd 任务各自 new 一个 Semaphore(_VISION_CONCURRENCY)，
# 叠加 pipeline(settings.VISION_CONCURRENCY=3) × job_manager(max_concurrent=10)，
# 峰值 LLM 并发≈3×3，会把 DashScope QPM/并发上限打爆。
# 改为「一处全局信号量」，无论同时跑多少个 kbd / 多少个 job，总 LLM 并发恒定 ≤ 该值。
# 延迟到事件循环内创建，规避 asyncio 3.10+ 在循环外创建 Semaphore 的 DeprecationWarning。
async def _get_vision_semaphore() -> asyncio.Semaphore:
    """兼容旧调用方，但实际使用全局 KBD LLM 资源池。

    CLASSIFY/EXTRACT_SIGNALS 与 VISION 共用同一 Semaphore，避免阶段并行后
    各自占满 Provider 配额。
    """
    return await get_llm_semaphore()


# ──────────────────────────────────────────────────────────────────────────────
# Step 0：文档解析与上下文提取
# ──────────────────────────────────────────────────────────────────────────────

async def ensure_images_json_complete(
    kbd_entry: KbdEntry,
    kbd_images: list[KbdImage],
    db_session: AsyncSession,
) -> bool:
    """确保 images_json 包含 kbd_image 表中所有图片的条目。

    根因修复：解决历史数据中 images_json 只有部分 seq 的问题。
    确保 kbd_image 表中的每张图片在 images_json 中都有对应条目。

    Args:
        kbd_entry: KBD 条目对象
        kbd_images: kbd_image 表中的所有图片
        db_session: 数据库会话

    Returns:
        bool: 是否有新增条目（用于日志记录）
    """
    # 获取 kbd_image 表中的所有 seq
    referenced_seqs = {img.seq for img in kbd_images}

    if not referenced_seqs:
        return False

    # 获取 images_json 中已有的 seq
    existing_seqs = {item.get("seq") for item in (kbd_entry.images_json or []) if item.get("seq") is not None}

    # 找出缺失的 seq
    missing_seqs = referenced_seqs - existing_seqs

    if not missing_seqs:
        return False

    # 为缺失的 seq 创建占位条目
    images_json = [dict(item) for item in (kbd_entry.images_json or [])]
    for seq in sorted(missing_seqs):
        images_json.append({
            "seq": seq,
            "section": "steps_text",  # 默认归属排障步骤
            "context_before": "",
            "context_after": "",
            "desc": "",  # 占位，等待 Vision LLM 填充
        })

    # 按 seq 排序后写回
    images_json.sort(key=lambda x: x["seq"])
    kbd_entry.images_json = images_json
    _mark_signal_generation_stale(kbd_entry)

    logger.info(
        event="images_json_synced",
        kbd_entry_id=kbd_entry.id,
        missing_seqs=sorted(missing_seqs),
        total_seqs=len(images_json),
    )

    return True


def _strip_html(html: str) -> str:
    """去除 HTML 标签、&nbsp;、多余空白。"""
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _extract_context(html: str, img_pos: int) -> str:
    """提取图片上文（300字，不足则扩展到800字）。"""
    short_raw = html[max(0, img_pos - _SHORT_WINDOW):img_pos]
    short_text = _strip_html(short_raw).strip()
    if len(short_text) >= _MIN_CONTEXT_CHARS:
        return short_text[-_SHORT_WINDOW:]
    long_raw = html[max(0, img_pos - _LONG_WINDOW):img_pos]
    long_text = _strip_html(long_raw).strip()
    return long_text[-_LONG_WINDOW:]


def _build_context_map_from_html(html: str) -> dict[int, str]:
    """从 HTML 构建图片序号 -> 上下文文字映射。

    使用简单的 img 标签位置提取，不依赖外部 html_utils。
    """
    context_map: dict[int, str] = {}
    # 匹配 <img src="..." /> 标签位置
    for seq, m in enumerate(re.finditer(r"<img[^>]*src=[\"']([^\"']+)[\"'][^>]*/?>", html, re.IGNORECASE)):
        context_map[seq] = _extract_context(html, m.start())
    return context_map


def _build_context_map_from_images_json(images_json: list[dict[str, Any]] | None) -> dict[int, str]:
    """从 IMPORT 阶段持久化的截图前后文构建 Vision 上下文。

    ``content_md`` 是渲染视图，图片以 ``![img:N]`` 表示，不能再用 HTML ``<img>``
    正则可靠恢复上下文。``images_json`` 才是 seq、章节和上下文的单一事实来源。
    """

    result: dict[int, str] = {}
    for item in images_json or []:
        if not isinstance(item, dict) or item.get("seq") is None:
            continue
        before = str(item.get("context_before") or "").strip()
        after = str(item.get("context_after") or "").strip()
        parts = []
        if before:
            parts.append(f"【截图前文】{before}")
        if after:
            parts.append(f"【截图后文】{after}")
        if parts:
            result[int(item["seq"])] = "\n".join(parts)
    return result


_TYPE_TO_EVIDENCE = {
    "终端截图": ("terminal", "terminal"),
    "日志截图": ("log", "log"),
    "告警截图": ("alert", "alert"),
    "任务截图": ("task", "task"),
    # 弹框承载故障语义，但通常叠加在配置/任务页面上；surface 不能据此臆测。
    "弹框截图": ("unknown", "dialog"),
    "配置截图": ("config", "config"),
    "其他截图": ("unknown", "other"),
}

# DESCRIPTION 是单模型结合文档上下文生成的语义 Proposal，不是截图中可直接观察到的事实。
# 其中因果、根因、归责类表述风险最高：即使 OCR 完全正确，模型仍可能把上下游关系倒置。
# 这里不尝试用关键词“纠正”业务结论，只做可审计的风险标记；运行时信号仍只能消费
# observed_facts/text_lines/fields，不能消费 inferences。
_HIGH_RISK_INFERENCE_TERMS = (
    "根本原因",
    "根因",
    "导致",
    "引发",
    "造成",
    "因此",
    "从而",
    "直接原因",
    "必然",
    "确认是",
    "可确认",
)


def _assess_inference(description: str) -> tuple[str, bool, list[str]]:
    """评估自由文本语义推断的可用边界，不伪造模型置信度。

    返回 ``(status, needs_review, issues)``：
    - 没有 DESCRIPTION：not_present；
    - 普通语义概括：unverified，仍需与正文/其他证据交叉验证；
    - 含因果/根因断言：needs_review，并给出稳定问题码。

    无论返回哪种状态，DESCRIPTION 都不会晋升为 observed fact。
    """

    normalized = description.strip()
    if not normalized:
        return "not_present", False, []
    if any(term in normalized for term in _HIGH_RISK_INFERENCE_TERMS):
        return "needs_review", True, ["unsupported_causal_claim"]
    return "unverified", True, ["single_model_semantic_inference"]


def _mark_signal_generation_stale(kbd_entry: Any) -> None:
    """Vision Evidence 变化后保留旧契约供 diff，但禁止继续当作 current 执行。"""
    document = kbd_entry.signals_json
    if not isinstance(document, dict) or not isinstance(document.get("generation_metadata"), dict):
        return
    document = dict(document)
    metadata = dict(document["generation_metadata"])
    metadata["status"] = "stale"
    document["generation_metadata"] = metadata
    kbd_entry.signals_json = document


async def _freeze_vision_proposal(
    db_session: AsyncSession,
    *,
    kbd_entry: KbdEntry,
    prompt_template: str,
    trace_id: str | None,
    origin: str,
    scope: dict[str, Any],
    validation_summary: dict[str, Any],
) -> int:
    """用 KBD 统一 revision 服务冻结识图 Proposal。

    分类、识图、关键信号和专家审核共享 kbd_revision 的完整 payload、checksum、
    parent/baseline 与 head 维护规则。这里仅补充识图领域的模型/Prompt/输入指纹，
    不创建第二套视觉版本表或审核状态。
    """

    image_hashes: list[str] = []
    for item in kbd_entry.images_json or []:
        if not isinstance(item, dict):
            continue
        evidence = item.get("evidence")
        provenance = evidence.get("provenance") if isinstance(evidence, dict) else None
        image_hashes.append(
            str(provenance.get("image_sha256") or "") if isinstance(provenance, dict) else ""
        )
    image_hashes.sort()
    await db_session.flush()
    proposal_revision = await freeze_kbd_ai_proposal(
        db_session,
        kbd=kbd_entry,
        generation_kind="vision",
        origin=origin,
        generation_metadata={
            "model_id": _LLM_VISION_MODEL,
            "prompt_name": _KBD_VISION_PROMPT_NAME,
            "prompt_revision": hashlib.sha256(prompt_template.encode("utf-8")).hexdigest(),
            "input_hash": hashlib.sha256("\n".join(image_hashes).encode("utf-8")).hexdigest(),
            "scope": scope,
        },
        validation_summary=validation_summary,
        trace_id=trace_id,
    )
    return int(proposal_revision.id)


def _build_evidence_ir(
    *,
    seq: int,
    section: str,
    context_before: str,
    context_after: str,
    screenshot_type: str,
    full_text: list[str],
    description: str,
    image_data: bytes,
    prompt_template: str,
    status: str | None = None,
) -> dict[str, Any]:
    """把 Vision Proposal 封装成可追溯 Evidence IR，保留事实/推断边界。"""

    surface, evidence_type = _TYPE_TO_EVIDENCE.get(screenshot_type, ("unknown", "other"))
    if status is None:
        if full_text:
            status = "success"
        elif description:
            status = "partial"
        else:
            status = "low_quality"
    # needs_review 只描述“可观察证据抽取”是否可进入后续 Proposal。语义描述有独立的
    # inference_* 质量维度，避免把 OCR 成功与语义推断正确混成一个布尔值。
    needs_review = status in {"partial", "low_quality", "failed"} or evidence_type == "other"
    inference_status, inference_needs_review, inference_issues = _assess_inference(description)

    text_lines = [
        {"text": line, "confidence": None, "bbox": None}
        for line in full_text
        if line.strip()
    ]
    observed_facts = [f"截图可见文字：{line}" for line in full_text if line.strip()]
    # DESCRIPTION 是模型语义概括而非 OCR 原文，必须进入 inferences，不能伪装成可见事实。
    inferences = [description] if description else []

    inferred_mime = "image/png" if image_data.startswith(b"\x89PNG") else "image/jpeg"
    prepared_images = _prepare_vision_images(image_data, inferred_mime)
    transforms = [
        {
            "tile_id": f"tile_{index}",
            "bbox_source": list(bbox) if bbox is not None else None,
            "mime_type": prepared_mime,
            "bytes": len(prepared_data),
            "sha256": hashlib.sha256(prepared_data).hexdigest(),
        }
        for index, (prepared_data, prepared_mime, bbox) in enumerate(prepared_images)
    ]

    return {
        "schema_version": 1,
        "seq": seq,
        "section": section,
        "context_before": context_before,
        "context_after": context_after,
        "regions": [{
            "region_id": f"img_{seq}:r_0",
            "surface": surface,
            "evidence_type": evidence_type,
            "bbox": None,
            "text_lines": text_lines,
            "fields": {"text": "\n".join(full_text)},
            "observed_facts": observed_facts,
            "inferences": inferences,
        }],
        "quality": {
            # 当前模型不返回可信的标定置信度；None 明确表示未知，禁止伪造数值。
            "ocr_coverage": None,
            "type_confidence": None,
            "status": status,
            "needs_review": needs_review,
            "inference_status": inference_status,
            "inference_needs_review": inference_needs_review,
            "inference_issues": inference_issues,
        },
        "provenance": {
            "image_sha256": hashlib.sha256(image_data).hexdigest(),
            "ocr_model": _LLM_VISION_MODEL,
            "ocr_mode": "vision_llm",
            "vision_model": _LLM_VISION_MODEL,
            "prompt_revision": hashlib.sha256(prompt_template.encode("utf-8")).hexdigest()[:16],
            "transform": "tiled" if len(transforms) > 1 else "original-or-lossless-resize",
            "transforms": transforms,
        },
    }


# ──────────────────────────────────────────────────────────────────────────────
# 图片压缩（避免大图超时）
# ──────────────────────────────────────────────────────────────────────────────

def _compress_image_if_needed(image_data: bytes, mime_type: str) -> tuple[bytes, str]:
    """超限时保真优先缩放；文字 PNG 不再默认转成 quality=70 的 JPEG。"""
    if len(image_data) <= _MAX_VISION_IMAGE_SIZE:
        return image_data, mime_type

    try:
        from PIL import Image
    except ImportError:
        logger.warning("Pillow 未安装，使用原图（大小=%dKB）", len(image_data) // 1024)
        return image_data, mime_type

    try:
        img = Image.open(io.BytesIO(image_data))
        max_dimension = 2400
        if max(img.width, img.height) > max_dimension:
            ratio = max_dimension / max(img.width, img.height)
            img = img.resize(
                (max(1, int(img.width * ratio)), max(1, int(img.height * ratio))),
                Image.Resampling.LANCZOS,
            )
        buffer = io.BytesIO()
        if mime_type.lower() == "image/png":
            # UI/终端/日志文字边缘对有损压缩非常敏感，优先使用无损 PNG。
            img.save(buffer, format="PNG", optimize=True)
            output_mime = "image/png"
        else:
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")
            img.save(buffer, format="JPEG", quality=90, optimize=True)
            output_mime = "image/jpeg"
        compressed = buffer.getvalue()
        logger.info(
            "图片压缩 %dKB->%dKB",
            len(image_data) // 1024,
            len(compressed) // 1024,
        )
        return compressed, output_mime
    except Exception as exc:
        logger.warning("压缩失败：%s，使用原图", exc)
        return image_data, mime_type


def _prepare_vision_images(
    image_data: bytes,
    mime_type: str,
) -> list[tuple[bytes, str, tuple[int, int, int, int] | None]]:
    """为超长文字截图生成有重叠的多尺度切片，并保留源图坐标。"""
    try:
        from PIL import Image
    except ImportError:
        compressed, output_mime = _compress_image_if_needed(image_data, mime_type)
        return [(compressed, output_mime, None)]

    try:
        image = Image.open(io.BytesIO(image_data))
        width, height = image.size
        long_side = max(width, height)
        short_side = max(1, min(width, height))
        if long_side <= 2400 or long_side / short_side < 2.2:
            compressed, output_mime = _compress_image_if_needed(image_data, mime_type)
            return [(compressed, output_mime, (0, 0, width, height))]

        vertical = height >= width
        tile_extent = 1800
        overlap = 180
        step = tile_extent - overlap
        axis_length = height if vertical else width
        starts = [0]
        while starts[-1] + tile_extent < axis_length and len(starts) < 8:
            next_start = min(starts[-1] + step, axis_length - tile_extent)
            if next_start <= starts[-1]:
                break
            starts.append(next_start)

        tiles: list[tuple[bytes, str, tuple[int, int, int, int] | None]] = []
        for start in sorted(set(starts)):
            end = min(axis_length, start + tile_extent)
            bbox = (0, start, width, end) if vertical else (start, 0, end, height)
            crop = image.crop(bbox)
            buffer = io.BytesIO()
            if mime_type.lower() == "image/png":
                crop.save(buffer, format="PNG", optimize=True)
                output_mime = "image/png"
            else:
                if crop.mode not in ("RGB", "L"):
                    crop = crop.convert("RGB")
                crop.save(buffer, format="JPEG", quality=90, optimize=True)
                output_mime = "image/jpeg"
            tiles.append((buffer.getvalue(), output_mime, bbox))
        return tiles or [(image_data, mime_type, (0, 0, width, height))]
    except Exception as exc:
        logger.warning("图片切片失败：%s，回退单图保真压缩", exc)
        compressed, output_mime = _compress_image_if_needed(image_data, mime_type)
        return [(compressed, output_mime, None)]


# ──────────────────────────────────────────────────────────────────────────────
# Step 1：Vision LLM 单次调用
# ──────────────────────────────────────────────────────────────────────────────

async def _vision_analyze(
    client: AsyncOpenAI,
    image_data: bytes,
    mime_type: str,
    context: str,
    prompt_template: str,
    trace_id: str | None = None,
    kbd_entry_id: int | None = None,
    image_seq: int | None = None,
) -> tuple[str, str, list[str], str]:
    """
    Vision LLM 单次调用，输出 TYPE + BACKGROUND + FULL_TEXT + DESCRIPTION。

    Returns:
        (type, background, full_text_lines, description)
        失败时返回 ("其他截图", "其他", [], "")
    """
    started = time.perf_counter()
    prepared_images = _prepare_vision_images(image_data, mime_type)
    image_parts = [
        {
            "type": "image_url",
            "image_url": {
                "url": f"data:{prepared_mime};base64,{base64.b64encode(prepared_data).decode('utf-8')}"
            },
        }
        for prepared_data, prepared_mime, _ in prepared_images
    ]
    prompt = prompt_template.format(context=context or "（无上下文）")
    if len(prepared_images) > 1:
        prompt += (
            f"\n\n【多尺度切片说明】原图按视觉顺序拆为 {len(prepared_images)} 张有重叠切片；"
            "请跨切片去重，仍按原图从上到下、从左到右输出完整文字。"
        )

    def _fmt_vision_err(exc: Exception) -> str:
        if getattr(exc, "response", None) is not None:
            sc = getattr(exc.response, "status_code", "unknown")
            rt = getattr(exc.response, "text", "")
            return f"{exc} (status={sc}{(' body=' + rt[:300]) if rt else ''})"
        if isinstance(exc, httpx.HTTPStatusError):
            return f"HTTP {exc.response.status_code}: {exc.response.text[:500]}"
        return str(exc)

    # ── 带退避的重试（P-优化：解决限流/超时导致的批量必挂）──
    # 针对 429 限流、5xx、超时进行指数退避重试，并尊重响应头 Retry-After；
    # 鉴权/参数类错误（4xx 非 429）不重试，立即抛出以免浪费配额。
    max_attempts = max(1, int(os.environ.get("VISION_LLM_MAX_ATTEMPTS", "2")))
    last_exc: Exception | None = None
    raw = ""
    for attempt in range(1, max_attempts + 1):
        try:
            with observe_llm_generation(
                operation="vision",
                model=_LLM_VISION_MODEL,
                input={
                    "prompt": prompt,
                    "image_count": len(prepared_images),
                    "image_bytes": sum(len(prepared_data) for prepared_data, _, _ in prepared_images),
                },
                metadata={
                    "attempt": attempt,
                    "trace_id": trace_id or "",
                    "image_count": len(prepared_images),
                    "kbd_id": kbd_entry_id,
                    "image_seq": image_seq,
                    "prompt_name": _KBD_VISION_PROMPT_NAME,
                    "prompt_revision": hashlib.sha256(prompt_template.encode("utf-8")).hexdigest(),
                },
                model_parameters={
                    "temperature": 0.0,
                    "max_tokens": _VISION_MAX_TOKENS,
                    "enable_thinking": _LLM_ENABLE_THINKING,
                },
            ) as observation:
                response = await client.chat.completions.create(
                    model=_LLM_VISION_MODEL,
                    messages=[{
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            *image_parts,
                        ],
                    }],
                    max_tokens=_VISION_MAX_TOKENS,
                    temperature=0.0,
                    timeout=_LLM_VISION_TIMEOUT,
                    extra_body={"enable_thinking": _LLM_ENABLE_THINKING},
                )
                message = response.choices[0].message
                raw = (message.content or "").strip()
                if not raw:
                    # 部分 Ollama/OpenAI 兼容模型把最终文本放在扩展字段中。
                    model_extra = getattr(message, "model_extra", None) or {}
                    raw = str(
                        getattr(message, "reasoning_content", None)
                        or model_extra.get("reasoning_content")
                        or ""
                    ).strip()
                finish_reason = str(getattr(response.choices[0], "finish_reason", None) or "unknown")
                usage = getattr(response, "usage", None)
                prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
                completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
                tokens = int(getattr(usage, "total_tokens", 0) or 0)
                KBD_LLM_REQUESTS_TOTAL.labels(
                    operation="vision",
                    model=_LLM_VISION_MODEL,
                    status="success" if raw and finish_reason != "length" else "error",
                    finish_reason=finish_reason,
                ).inc()
                for token_type, value in (("input", prompt_tokens), ("output", completion_tokens), ("total", tokens)):
                    if value:
                        KBD_LLM_TOKENS_TOTAL.labels(
                            operation="vision", model=_LLM_VISION_MODEL, type=token_type
                        ).inc(value)
                update_observation(
                    observation,
                    output={"content": raw},
                    metadata={"finish_reason": finish_reason, "response_chars": len(raw), "attempt": attempt},
                    usage_details={"input": prompt_tokens, "output": completion_tokens, "total": tokens},
                    level="ERROR" if not raw or finish_reason == "length" else None,
                    status_message="识图结果为空或被截断" if not raw or finish_reason == "length" else None,
                )
                logger.info(
                    event="vision_llm_response",
                    trace_id=trace_id,
                    kbd_entry_id=kbd_entry_id,
                    image_seq=image_seq,
                    model=_LLM_VISION_MODEL,
                    attempt=attempt,
                    finish_reason=finish_reason,
                    response_chars=len(raw),
                    response_sha256=hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest(),
                    image_sha256=hashlib.sha256(image_data).hexdigest(),
                    prepared_image_count=len(prepared_images),
                    prepared_image_bytes=sum(len(item[0]) for item in prepared_images),
                    total_tokens=tokens,
                )
                if finish_reason == "length":
                    raise VisionEmptyResultError("Vision 模型输出达到长度上限，结果不完整")
                logger.debug("Vision LLM 响应 tokens=%d", tokens)
            last_exc = None
            break  # 成功，跳出重试
        except Exception as exc:
            last_exc = exc
            status_code = getattr(getattr(exc, "response", None), "status_code", None)
            is_rate_limit = status_code == 429
            is_server = status_code is not None and 500 <= status_code < 600
            is_timeout = isinstance(exc, (httpx.TimeoutException, asyncio.TimeoutError, APITimeoutError, APIConnectionError))
            # 非限流/非服务端/非超时的错误（如 401 鉴权失败）不重试
            if not (is_rate_limit or is_server or is_timeout):
                if not isinstance(exc, VisionEmptyResultError):
                    KBD_LLM_REQUESTS_TOTAL.labels(
                        operation="vision",
                        model=_LLM_VISION_MODEL,
                        status="error",
                        finish_reason="transport_error",
                    ).inc()
                logger.exception(
                    event="vision_llm_error_unretriable",
                    message=_fmt_vision_err(exc),
                    trace_id=trace_id,
                )
                raise
            if attempt == max_attempts:
                KBD_LLM_REQUESTS_TOTAL.labels(
                    operation="vision",
                    model=_LLM_VISION_MODEL,
                    status="error",
                    finish_reason="transport_error",
                ).inc()
                logger.exception(
                    event="vision_llm_error_gave_up",
                    message=f"已重试 {max_attempts} 次后放弃: {_fmt_vision_err(exc)}",
                    trace_id=trace_id,
                )
                raise
            # 退避：优先 Retry-After，否则指数退避（上限 30s）
            retry_after = None
            if is_rate_limit and getattr(exc, "response", None) is not None:
                ra = exc.response.headers.get("Retry-After")  # type: ignore[union-attr]
                if ra and str(ra).isdigit():
                    retry_after = float(ra)
            wait = retry_after if retry_after else min(2.0 * (2 ** (attempt - 1)), 30.0)
            logger.warning(
                event="vision_llm_retry",
                message=(
                    f"第 {attempt}/{max_attempts} 次调用失败（{type(exc).__name__}，status={status_code}）："
                    f"{_fmt_vision_err(exc)[:300]}；{wait:.1f}s 后重试"
                ),
                trace_id=trace_id,
            )
            await asyncio.sleep(wait)
    if last_exc is not None:
        raise last_exc  # 兜底（理论上不可达，上面已 raise）

    screenshot_type = _parse_type(raw)
    background = _parse_background(raw)
    full_text = _parse_full_text(raw)
    description = _parse_description(raw)
    if not full_text and not description:
        full_text = _parse_unstructured_ocr_text(raw)
        if full_text:
            logger.info(
                event="vision_unstructured_ocr_fallback",
                message="模型未返回约定分区，已将非结构化 OCR 文本作为可见文字保存",
                raw_length=len(raw),
                line_count=len(full_text),
                trace_id=trace_id,
            )
    screenshot_type = _prefer_task_detail_type(screenshot_type, full_text)

    logger.info(
        event="vision_llm_parsed",
        trace_id=trace_id,
        kbd_entry_id=kbd_entry_id,
        image_seq=image_seq,
        model=_LLM_VISION_MODEL,
        screenshot_type=screenshot_type,
        ocr_line_count=len(full_text),
        ocr_chars=sum(len(line) for line in full_text),
        description_chars=len(description),
        duration_ms=round((time.perf_counter() - started) * 1000, 3),
    )

    return screenshot_type, background, full_text, description


# ──────────────────────────────────────────────────────────────────────────────
# 输出解析（正则）
# ──────────────────────────────────────────────────────────────────────────────

def _parse_type(raw: str) -> str:
    m = re.search(r"TYPE[:\s]+(终端截图|日志截图|告警截图|任务截图|弹框截图|配置截图|其他截图)", raw)
    return m.group(1) if m else "其他截图"


def _parse_background(raw: str) -> str:
    m = re.search(r"BACKGROUND[:\s]+(白色|黑色|灰色|彩色|其他)", raw)
    return m.group(1) if m else "其他"


def _parse_full_text(raw: str) -> list[str]:
    """解析 FULL_TEXT section 的 bullet 行。"""
    ft_start = raw.find("FULL_TEXT:")
    if ft_start == -1:
        return []

    next_section = raw.find("DESCRIPTION:", ft_start)
    if next_section == -1:
        next_section = raw.find("════════", ft_start)
    if next_section == -1:
        next_section = len(raw)

    ft_section = raw[ft_start:next_section]

    lines = []
    for line in ft_section.split("\n"):
        line = line.strip()
        if line.startswith("- "):
            content = line[2:].strip()
            if content and content not in ("（无文字）", "(无文字)"):
                lines.append(content)

    return lines


def _parse_unstructured_ocr_text(raw: str) -> list[str]:
    """兼容只返回纯 OCR/Markdown 文本、不遵循分区协议的专用 OCR 模型。

    该回退只接受完全不含约定分区头的响应，避免把 ``TYPE`` 等控制字段误当成
    截图可见文字。文本只作为 observed text（可见文字）保存，不生成语义推断。
    """

    normalized = raw.strip()
    if not normalized or re.search(r"(?:^|\n)\s*(?:TYPE|BACKGROUND|FULL_TEXT|DESCRIPTION)\s*:", normalized):
        return []
    normalized = re.sub(r"<\|[^>]+\|>", "", normalized)
    normalized = re.sub(r"^```(?:markdown|text)?\s*|\s*```$", "", normalized, flags=re.IGNORECASE)
    lines: list[str] = []
    for source_line in normalized.splitlines():
        line = source_line.strip()
        line = re.sub(r"^(?:[-*+]\s+|\d+[.)]\s+)", "", line).strip()
        if not line or line in {"（无文字）", "(无文字)", "无文字"}:
            continue
        lines.append(line[:1000])
        if len(lines) >= 200:
            break
    return lines


def _require_nonempty_vision_result(full_text: list[str], description: str) -> None:
    """禁止把连续空响应记为识图成功并覆盖既有证据。"""

    if not full_text and not description:
        raise VisionEmptyResultError("Vision 模型连续返回空结果或不受支持的输出格式")


async def _emit_progress(
    on_progress: Callable[[int, int, int], Awaitable[None] | None] | None,
    done: int,
    failed: int,
    total: int,
) -> None:
    """兼容同步与异步进度回调，确保图片级进度已经落库再继续。"""

    if on_progress is None:
        return
    result = on_progress(done, failed, total)
    if result is not None:
        await result


def _prefer_task_detail_type(screenshot_type: str, full_text: list[str]) -> str:
    """将有完整可见任务字段的详情弹窗归类为任务截图。

    Vision 模型可能被“弹窗”这一视觉外观干扰。这里不读取 DESCRIPTION 或文档上下文，
    只检查 OCR 的可见字段：同时具备状态、行为，且另有时间/对象等至少一个任务字段
    时，语义已足以确定为任务详情。普通错误弹窗通常不满足该组合，仍保留弹框截图。
    """

    if screenshot_type not in {"弹框截图", "配置截图", "其他截图"}:
        return screenshot_type
    text = "\n".join(str(line) for line in full_text)
    if not all(field in text for field in _TASK_DETAIL_REQUIRED_FIELDS):
        return screenshot_type
    if not any(field in text for field in _TASK_DETAIL_SUPPORTING_FIELDS):
        return screenshot_type
    return "任务截图"


def _parse_description(raw: str) -> str:
    """解析 DESCRIPTION 字段。"""
    desc_start = raw.find("DESCRIPTION:")
    if desc_start == -1:
        return ""

    rest = raw[desc_start + 12:]

    end_pos = len(rest)
    for pattern in ["\nTYPE:", "\nBACKGROUND:", "\nFULL_TEXT:", "\n═══"]:
        idx = rest.find(pattern)
        if idx != -1 and idx < end_pos:
            end_pos = idx

    desc = rest[:end_pos].strip()
    desc = re.sub(r"═══.*$", "", desc).strip()
    return desc if desc and desc not in ("（无描述）", "(无描述)") else ""


def _format_desc(screenshot_type: str, background: str, full_text: list[str], description: str) -> str:
    """组装 desc.txt 格式（与 data-pipeline 保持兼容）。"""
    lines = [
        f"TYPE: {screenshot_type}",
        f"BACKGROUND: {background}",
        "FULL_TEXT:",
    ]
    for line in full_text:
        lines.append(f"- {line}")
    if not full_text:
        lines.append("- （无文字）")
    lines.append("DESCRIPTION:")
    lines.append(description if description else "（无描述）")
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────────────
# 核心重算入口
# ──────────────────────────────────────────────────────────────────────────────

async def reanalyze_kbd_images(
    kbd_entry_id: int,
    session_factory: Callable[[], AsyncSession],
    on_progress: Callable[[int, int, int], Awaitable[None] | None] | None = None,
    trace_id: str | None = None,
) -> dict[str, Any]:
    """重新识图单个 KBD 条目的所有图片。

    流程：
      1. 从 system_prompt 表热加载 Vision Prompt（kbd_vision_v1）
      2. 查询 kbd_image 表获取该 KBD 的所有原始图片
      3. 查询 kbd_entry.images_json 获取 IMPORT 阶段持久化的截图上下文
      4. 并发调用 Vision LLM（Semaphore 控制）
      5. 组装 images_json，更新 kbd_entry.images_json
      6. 调用 kbd_entry.rebuild_content_md() 重建 content_md
      7. 返回处理统计

    Args:
        kbd_entry_id: KBD 条目 ID
        session_factory: SQLAlchemy AsyncSession 工厂函数

    Returns:
        {
            "kbd_entry_id": int,
            "total": int,       # 图片总数
            "done": int,        # 成功数
            "failed": int,      # 失败数
        "images_json": list,  # 新的 images_json
    }
    """
    # 贯穿全链路的 trace_id：优先使用调用方透传的（来自 data-pipeline 的 traceparent），
    # 否则回退到 OTel 当前上下文（后台 task 可能脱离请求 span，故以透传为准）。
    _tid = trace_id or get_current_trace_id()

    # 1. 查询阶段：使用一个 short-lived session 获取数据并加载 Prompt
    async with session_factory() as db_session:
        # 加载 Vision Prompt
        prompt_template = await StrictPromptLoader.load_and_validate(
            db_session,
            _KBD_VISION_PROMPT_NAME,
            ["context"],
            consumer="kb-service.vision_processor",
        )

        # 查询 KBD 条目
        entry_result = await db_session.execute(
            select(KbdEntry).where(KbdEntry.id == kbd_entry_id)
        )
        kbd_entry = entry_result.scalar_one_or_none()
        if kbd_entry is None:
            raise ValueError(f"KBD 条目 {kbd_entry_id} 不存在")

        # 查询所有原始图片
        images_result = await db_session.execute(
            select(KbdImage)
            .where(KbdImage.kbd_entry_id == kbd_entry_id)
            .order_by(KbdImage.seq)
        )
        kbd_images: list[KbdImage] = list(images_result.scalars().all())

        if not kbd_images:
            logger.warning(
                event="reanalyze_kbd_images_no_images",
                kbd_entry_id=kbd_entry_id,
                message="该 KBD 无原始图片，无法重算识图（可能为存量数据）",
            )
            return {
                "kbd_entry_id": kbd_entry_id,
                "total": 0,
                "done": 0,
                "failed": 0,
                "success": True,
                "images_json": [],
                "message": "该 KBD 无原始图片，无法重算识图",
            }

        # 将图片数据和 Evidence 元数据拉入内存，避免 DB session 关闭后的 ORM 懒加载。
        context_source = kbd_entry.content_md or ""
        existing_images_json = [
            dict(item) for item in (kbd_entry.images_json or []) if isinstance(item, dict)
        ]
        image_items = []
        for img in kbd_images:
            image_items.append({
                "seq": img.seq,
                "mime_type": img.mime_type or "image/png",
                "image_data": img.image_data,
            })

    # 2. 新数据直接使用 IMPORT 阶段持久化上下文；HTML 解析仅为存量数据兼容兜底。
    context_map = _build_context_map_from_images_json(existing_images_json)
    if not context_map:
        context_map = _build_context_map_from_html(context_source)

    existing_by_seq = {
        int(item["seq"]): item
        for item in existing_images_json
        if item.get("seq") is not None
    }

    # 保留 IMPORT 阶段已算出的章节归属（seq → section），避免 VISION 重建时退化统一为 steps_text。
    section_by_seq = {
        item.get("seq"): (item.get("section") or "steps_text")
        for item in existing_images_json
        if item.get("seq") is not None
    }

    # 3. 创建 LLM 客户端
    if not _LLM_VISION_API_KEY:
        raise RuntimeError("VISION_API_KEY 或 API_KEY 未配置，无法调用 Vision LLM")

    client = AsyncOpenAI(
        api_key=_LLM_VISION_API_KEY,
        base_url=_LLM_VISION_BASE_URL,
        timeout=_LLM_VISION_TIMEOUT,
        # 重试统一由 _vision_analyze 治理，避免 SDK 与业务层重试次数相乘。
        max_retries=0,
    )

    # 4. 并发处理所有图片（不占有任何 DB 连接）
    # 使用全局共享信号量，统一收敛 LLM 并发（见 _get_vision_semaphore 注释），
    # 避免「job_manager × pipeline × processor」三重信号量叠加放大打爆 DashScope QPM。
    sem = await _get_vision_semaphore()
    stats = {"done": 0, "failed": 0}
    errors: list[str] = []  # 收集每张图片的真实失败原因（透传给调用方）
    images_json: list[dict[str, Any]] = []
    images_json_lock = asyncio.Lock()

    async def _process_one(image_item: dict[str, Any]) -> None:
        seq = image_item["seq"]
        context = context_map.get(seq, "")
        # 沿用 IMPORT 阶段算出的章节（而非硬编码 steps_text），保证按章节检索/渲染的准确性
        section = section_by_seq.get(seq, "steps_text")
        old_item = existing_by_seq.get(seq, {})
        context_before = str(old_item.get("context_before") or "")
        context_after = str(old_item.get("context_after") or "")

        async with sem:
            try:
                mime_type = image_item["mime_type"]
                screenshot_type, background, full_text, description = await _vision_analyze(
                    client,
                    image_item["image_data"],
                    mime_type,
                    context,
                    prompt_template,
                    trace_id=_tid,
                    kbd_entry_id=kbd_entry_id,
                    image_seq=seq,
                )

                # 空结果只额外重试一次；连续为空必须失败，不能生成“无文字/无描述”的假成功。
                if not full_text and not description:
                    logger.warning(
                        event="vision_empty_result_retry",
                        kbd_entry_id=kbd_entry_id,
                        seq=seq,
                    )
                    screenshot_type, background, full_text, description = await _vision_analyze(
                        client,
                        image_item["image_data"],
                        mime_type,
                        context,
                        prompt_template,
                        trace_id=_tid,
                        kbd_entry_id=kbd_entry_id,
                        image_seq=seq,
                    )
                _require_nonempty_vision_result(full_text, description)

                desc = _format_desc(screenshot_type, background, full_text, description)
                evidence = _build_evidence_ir(
                    seq=seq,
                    section=section,
                    context_before=context_before,
                    context_after=context_after,
                    screenshot_type=screenshot_type,
                    full_text=full_text,
                    description=description,
                    image_data=image_item["image_data"],
                    prompt_template=prompt_template,
                )

                async with images_json_lock:
                    images_json.append({
                        "seq": seq,
                        "section": section,
                        "context_before": context_before,
                        "context_after": context_after,
                        "desc": desc,
                        "evidence": evidence,
                    })

                stats["done"] += 1
                logger.info(
                    event="vision_image_done",
                    kbd_entry_id=kbd_entry_id,
                    seq=seq,
                    screenshot_type=screenshot_type,
                    evidence_status=evidence["quality"]["status"],
                    needs_review=evidence["quality"]["needs_review"],
                    inference_status=evidence["quality"]["inference_status"],
                    inference_needs_review=evidence["quality"]["inference_needs_review"],
                    ocr_line_count=len(full_text),
                    ocr_chars=sum(len(line) for line in full_text),
                    image_sha256=evidence["provenance"]["image_sha256"],
                    prompt_revision=evidence["provenance"]["prompt_revision"],
                    trace_id=_tid,
                )
                await _emit_progress(on_progress, stats["done"], stats["failed"], len(image_items))
            except Exception as exc:
                stats["failed"] += 1
                errors.append(f"seq={seq}: {exc}")
                # 失败图片只放入本次内存结果用于诊断；整条 KBD 不会写回，避免覆盖既有证据。
                async with images_json_lock:
                    images_json.append({
                        "seq": seq,
                        "section": section,
                        "context_before": context_before,
                        "context_after": context_after,
                        "desc": "",
                        "evidence": _build_evidence_ir(
                            seq=seq,
                            section=section,
                            context_before=context_before,
                            context_after=context_after,
                            screenshot_type="其他截图",
                            full_text=[],
                            description="",
                            image_data=image_item["image_data"],
                            prompt_template=prompt_template,
                            status="failed",
                        ),
                    })
                logger.exception(
                    event="vision_image_failed",
                    kbd_entry_id=kbd_entry_id,
                    seq=seq,
                    error=exc,
                    error_code="VISION_IMAGE_PROCESSING_FAILED",
                    trace_id=_tid,
                )
                await _emit_progress(on_progress, stats["done"], stats["failed"], len(image_items))

    logger.info(
        event="vision_reanalyze_start",
        kbd_id=kbd_entry_id,
        image_count=len(image_items),
        trace_id=_tid,
    )
    await _emit_progress(on_progress, 0, 0, len(image_items))
    await asyncio.gather(*[_process_one(img) for img in image_items])

    # 5. 按 seq 排序
    images_json.sort(key=lambda x: x["seq"])

    # 全量重识图采用原子写回：只要任一图片失败，本轮所有结果都不覆盖既有证据。
    if stats["failed"] > 0:
        logger.warning(
            event="vision_reanalyze_not_committed",
            message="存在识图失败，本轮结果未写回，原有图片证据保持不变",
            kbd_entry_id=kbd_entry_id,
            total=len(image_items),
            done=stats["done"],
            failed=stats["failed"],
            trace_id=_tid,
        )
        return {
            "kbd_entry_id": kbd_entry_id,
            "total": len(image_items),
            "done": stats["done"],
            "failed": stats["failed"],
            "success": False,
            "images_json": existing_images_json,
            "proposal_revision_id": None,
            "error": "; ".join(errors) if errors else "存在识图失败，本轮结果未写回",
        }

    # 6. 写入阶段：再开启一个 short-lived session 写入数据库（重新查询并更新）
    proposal_revision_id: int | None = None
    async with session_factory() as db_session:
        try:
            kbd_entry = await require_mutable_kbd(db_session, kbd_entry_id, for_update=True)
            old_images = list(kbd_entry.images_json or [])
            kbd_entry.images_json = images_json
            _mark_signal_generation_stale(kbd_entry)
            kbd_entry.content_md = kbd_entry.rebuild_content_md(old_images_json=old_images)
            kbd_entry.sync_sections_from_content_md()
            proposal_revision_id = await _freeze_vision_proposal(
                db_session,
                kbd_entry=kbd_entry,
                prompt_template=prompt_template,
                trace_id=_tid,
                origin="vision_reanalyze",
                scope={"mode": "all", "seqs": [item["seq"] for item in images_json]},
                validation_summary={
                    "status": "passed" if stats["failed"] == 0 else "needs_review",
                    "total": len(image_items),
                    "done": stats["done"],
                    "failed": stats["failed"],
                },
            )
            await db_session.commit()
        except Exception as e:
            logger.warning(
                event="vision_commit_retry",
                kbd_entry_id=kbd_entry_id,
                error=str(e),
                trace_id=_tid,
            )
            # 连接可能已关闭，回滚后重新尝试一次
            await db_session.rollback()
            try:
                kbd_entry = await require_mutable_kbd(db_session, kbd_entry_id, for_update=True)
                old_images = list(kbd_entry.images_json or [])
                kbd_entry.images_json = images_json
                _mark_signal_generation_stale(kbd_entry)
                kbd_entry.content_md = kbd_entry.rebuild_content_md(old_images_json=old_images)
                kbd_entry.sync_sections_from_content_md()
                proposal_revision_id = await _freeze_vision_proposal(
                    db_session,
                    kbd_entry=kbd_entry,
                    prompt_template=prompt_template,
                    trace_id=_tid,
                    origin="vision_reanalyze",
                    scope={"mode": "all", "seqs": [item["seq"] for item in images_json]},
                    validation_summary={
                        "status": "passed" if stats["failed"] == 0 else "needs_review",
                        "total": len(image_items),
                        "done": stats["done"],
                        "failed": stats["failed"],
                    },
                )
                await db_session.commit()
            except Exception as retry_err:
                logger.error(
                    event="vision_commit_failed",
                    kbd_entry_id=kbd_entry_id,
                    error=str(retry_err),
                    trace_id=_tid,
                )
                return {
                    "kbd_entry_id": kbd_entry_id,
                    "total": len(image_items),
                    "done": stats["done"],
                    "failed": stats["failed"],
                    "success": False,
                    "images_json": images_json,
                    "error": f"数据库更新失败: {retry_err}",
                }

    logger.info(
        event="reanalyze_kbd_images_completed",
        kbd_entry_id=kbd_entry_id,
        total=len(image_items),
        done=stats["done"],
        failed=stats["failed"],
        trace_id=_tid,
    )

    return {
        "kbd_entry_id": kbd_entry_id,
        "total": len(image_items),
        "done": stats["done"],
        "failed": stats["failed"],
        "success": stats["failed"] == 0,
        "images_json": images_json,
        "proposal_revision_id": proposal_revision_id,
        # 透传真实失败原因（供 job_manager / 调用方诊断，避免只看到笼统的「Job 执行失败」）
        "error": "; ".join(errors) if errors else None,
    }


async def reanalyze_single_image(
    kbd_entry_id: int,
    seq: int,
    session_factory: Callable[[], AsyncSession],
    trace_id: str | None = None,
) -> dict[str, Any]:
    """重新识图单个 KBD 条目的指定图片。

    流程：
      1. 从 system_prompt 表热加载 Vision Prompt（kbd_vision_v1）
      2. 查询 kbd_image 表获取指定 seq 的原始图片
      3. 查询 kbd_entry.images_json 获取 IMPORT 阶段持久化的截图上下文
      4. 调用 Vision LLM（单次调用，无并发）
      5. 更新 images_json 中对应 seq 的 desc 字段
      6. 调用 kbd_entry.rebuild_content_md() 重建 content_md
      7. 返回处理结果

    Args:
        kbd_entry_id: KBD 条目 ID
        seq: 图片序号（从 0 开始）
        session_factory: SQLAlchemy AsyncSession 工厂函数

    Returns:
        {
            "kbd_entry_id": int,
            "seq": int,
            "screenshot_type": str,
            "background": str,
            "full_text": list[str],
            "description": str,
            "desc": str,  # 完整 desc.txt 格式
            "images_json": list,  # 更新后的 images_json
        }
    """
    # 贯穿全链路的 trace_id（优先调用方透传，否则回退 OTel 上下文）
    _tid = trace_id or get_current_trace_id()
    logger.info(event="vision_single_reanalyze_start", kbd_id=kbd_entry_id, seq=seq, trace_id=_tid)

    # 1. 查询阶段：使用一个 short-lived session 获取数据并加载 Prompt
    async with session_factory() as db_session:
        # 加载 Vision Prompt
        prompt_template = await StrictPromptLoader.load_and_validate(
            db_session,
            _KBD_VISION_PROMPT_NAME,
            ["context"],
            consumer="kb-service.vision_processor",
        )

        # 查询 KBD 条目
        entry_result = await db_session.execute(
            select(KbdEntry).where(KbdEntry.id == kbd_entry_id)
        )
        kbd_entry = entry_result.scalar_one_or_none()
        if kbd_entry is None:
            raise ValueError(f"KBD 条目 {kbd_entry_id} 不存在")

        # 查询指定 seq 的原始图片
        image_result = await db_session.execute(
            select(KbdImage)
            .where(KbdImage.kbd_entry_id == kbd_entry_id)
            .where(KbdImage.seq == seq)
        )
        kbd_image = image_result.scalar_one_or_none()
        if kbd_image is None:
            raise ValueError(f"KBD 条目 {kbd_entry_id} 的图片 seq={seq} 不存在")

        # 查询所有图片，确保 images_json 同步
        all_images_result = await db_session.execute(
            select(KbdImage)
            .where(KbdImage.kbd_entry_id == kbd_entry_id)
            .order_by(KbdImage.seq)
        )
        all_kbd_images: list[KbdImage] = list(all_images_result.scalars().all())

        # 根因修复：确保 images_json 包含所有 kbd_image 表中的图片
        await ensure_images_json_complete(kbd_entry, all_kbd_images, db_session)
        await db_session.commit()

        # 将必要的数据拉入内存（重新查询以获取同步后的 images_json）
        await db_session.refresh(kbd_entry)
        image_data = kbd_image.image_data
        mime_type = kbd_image.mime_type or "image/png"
        context_source = kbd_entry.content_md or ""
        existing_images_json = [
            dict(item) for item in (kbd_entry.images_json or []) if isinstance(item, dict)
        ]

    # 沿用 IMPORT 阶段算出的章节（而非硬编码 steps_text），保证单图刷新后章节不退化
    old_section = "steps_text"
    old_item: dict[str, Any] = {}
    for _it in existing_images_json:
        if _it.get("seq") == seq:
            old_section = _it.get("section") or "steps_text"
            old_item = _it
            break

    # 2. 构建图片上下文与调用 Vision LLM（不占有任何 DB 连接）
    context_map = _build_context_map_from_images_json(existing_images_json)
    if seq not in context_map:
        context_map.update(_build_context_map_from_html(context_source))
    context = context_map.get(seq, "")

    # 创建 LLM 客户端
    if not _LLM_VISION_API_KEY:
        raise RuntimeError("VISION_API_KEY 或 API_KEY 未配置，无法调用 Vision LLM")

    client = AsyncOpenAI(
        api_key=_LLM_VISION_API_KEY,
        base_url=_LLM_VISION_BASE_URL,
        timeout=_LLM_VISION_TIMEOUT,
        # 重试统一由 _vision_analyze 治理，避免 SDK 与业务层重试次数相乘。
        max_retries=0,
    )

    # 调用 Vision LLM
    screenshot_type, background, full_text, description = await _vision_analyze(
        client,
        image_data,
        mime_type,
        context,
        prompt_template,
        trace_id=_tid,
        kbd_entry_id=kbd_entry_id,
        image_seq=seq,
    )

    # 空结果重试一次
    if not full_text and not description:
        logger.warning(
            event="vision_empty_result_retry",
            kbd_entry_id=kbd_entry_id,
            seq=seq,
            trace_id=_tid,
        )
        screenshot_type, background, full_text, description = await _vision_analyze(
            client,
            image_data,
            mime_type,
            context,
            prompt_template,
            trace_id=_tid,
            kbd_entry_id=kbd_entry_id,
            image_seq=seq,
        )
    _require_nonempty_vision_result(full_text, description)

    # 3. 组装 desc
    desc = _format_desc(screenshot_type, background, full_text, description)
    evidence = _build_evidence_ir(
        seq=seq,
        section=old_section,
        context_before=str(old_item.get("context_before") or ""),
        context_after=str(old_item.get("context_after") or ""),
        screenshot_type=screenshot_type,
        full_text=full_text,
        description=description,
        image_data=image_data,
        prompt_template=prompt_template,
    )

    # 4. 写入阶段：再开启一个 short-lived session 写入数据库（重新查询并更新，防止断线）
    proposal_revision_id: int | None = None
    async with session_factory() as db_session:
        try:
            kbd_entry = await require_mutable_kbd(db_session, kbd_entry_id, for_update=True)

            old_images = list(kbd_entry.images_json or [])
            # 更新 images_json（保留其他图片的描述，仅更新当前 seq）
            images_json = [dict(item) for item in (kbd_entry.images_json or [])]
            section = old_section

            found = False
            for item in images_json:
                if item.get("seq") == seq:
                    item["desc"] = desc
                    item["section"] = section
                    item["evidence"] = evidence
                    found = True
                    break

            if not found:
                images_json.append({
                    "seq": seq,
                    "section": section,
                    "context_before": str(old_item.get("context_before") or ""),
                    "context_after": str(old_item.get("context_after") or ""),
                    "desc": desc,
                    "evidence": evidence,
                })

            images_json.sort(key=lambda x: x["seq"])
            kbd_entry.images_json = images_json
            _mark_signal_generation_stale(kbd_entry)
            kbd_entry.content_md = kbd_entry.rebuild_content_md(old_images_json=old_images)
            kbd_entry.sync_sections_from_content_md()
            proposal_revision_id = await _freeze_vision_proposal(
                db_session,
                kbd_entry=kbd_entry,
                prompt_template=prompt_template,
                trace_id=_tid,
                origin="vision_reanalyze_single",
                scope={"mode": "single", "seqs": [seq]},
                validation_summary={
                    "status": (
                        "needs_review"
                        if evidence["quality"]["needs_review"]
                        or evidence["quality"]["inference_needs_review"]
                        else "passed"
                    ),
                    "total": 1,
                    "done": 1,
                    "failed": 0,
                    "seq": seq,
                },
            )
            await db_session.commit()
        except Exception as e:
            logger.warning(
                event="vision_single_commit_retry",
                kbd_entry_id=kbd_entry_id,
                seq=seq,
                error=str(e),
            )
            await db_session.rollback()
            try:
                kbd_entry = await require_mutable_kbd(db_session, kbd_entry_id, for_update=True)
                old_images = list(kbd_entry.images_json or [])
                images_json = [dict(item) for item in (kbd_entry.images_json or [])]
                section = old_section

                found = False
                for item in images_json:
                    if item.get("seq") == seq:
                        item["desc"] = desc
                        item["section"] = section
                        item["evidence"] = evidence
                        found = True
                        break

                if not found:
                    images_json.append({
                        "seq": seq,
                        "section": section,
                        "context_before": str(old_item.get("context_before") or ""),
                        "context_after": str(old_item.get("context_after") or ""),
                        "desc": desc,
                        "evidence": evidence,
                    })

                images_json.sort(key=lambda x: x["seq"])
                kbd_entry.images_json = images_json
                _mark_signal_generation_stale(kbd_entry)
                kbd_entry.content_md = kbd_entry.rebuild_content_md(old_images_json=old_images)
                kbd_entry.sync_sections_from_content_md()
                proposal_revision_id = await _freeze_vision_proposal(
                    db_session,
                    kbd_entry=kbd_entry,
                    prompt_template=prompt_template,
                    trace_id=_tid,
                    origin="vision_reanalyze_single",
                    scope={"mode": "single", "seqs": [seq]},
                    validation_summary={
                        "status": (
                            "needs_review"
                            if evidence["quality"]["needs_review"]
                            or evidence["quality"]["inference_needs_review"]
                            else "passed"
                        ),
                        "total": 1,
                        "done": 1,
                        "failed": 0,
                        "seq": seq,
                    },
                )
                await db_session.commit()
            except Exception as retry_err:
                logger.error(
                    event="vision_single_commit_failed",
                    kbd_entry_id=kbd_entry_id,
                    seq=seq,
                    error=str(retry_err),
                )
                raise RuntimeError(f"数据库更新失败: {retry_err}") from retry_err

    logger.info(
        event="reanalyze_single_image_completed",
        kbd_entry_id=kbd_entry_id,
        seq=seq,
        screenshot_type=screenshot_type,
        background=background,
        evidence_status=evidence["quality"]["status"],
        needs_review=evidence["quality"]["needs_review"],
        inference_status=evidence["quality"]["inference_status"],
        inference_needs_review=evidence["quality"]["inference_needs_review"],
        ocr_line_count=len(full_text),
        ocr_chars=sum(len(line) for line in full_text),
        image_sha256=evidence["provenance"]["image_sha256"],
        prompt_revision=evidence["provenance"]["prompt_revision"],
        trace_id=_tid,
    )

    return {
        "kbd_entry_id": kbd_entry_id,
        "seq": seq,
        "screenshot_type": screenshot_type,
        "background": background,
        "full_text": full_text,
        "description": description,
        "desc": desc,
        "images_json": images_json,
        "proposal_revision_id": proposal_revision_id,
    }
