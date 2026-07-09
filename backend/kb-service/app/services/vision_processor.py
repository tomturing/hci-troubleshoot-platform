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
  - 并发控制：Semaphore 限制同时调用的 Vision LLM 数量
"""

from __future__ import annotations

import asyncio
import base64
import io
import os
import re
from typing import Any

from openai import AsyncOpenAI
from shared.observability.logger import get_logger
from shared.utils.prompt_loader import StrictPromptLoader
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.kbd_entry import KbdEntry, KbdImage

logger = get_logger("kb-service-vision-processor")

# ──────────────────────────────────────────────────────────────────────────────
# 常量配置
# ──────────────────────────────────────────────────────────────────────────────

_MIN_CONTEXT_CHARS = 80
_SHORT_WINDOW = 300
_LONG_WINDOW = 800
_MAX_VISION_IMAGE_SIZE = 1024 * 1024  # 1MB，超过需压缩
_VISION_CONCURRENCY = 3  # 并发 LLM 调用数

# 截图类型与背景颜色（LLM 输出标准）
_SCREENSHOT_TYPES = ("终端截图", "日志截图", "告警截图", "任务截图", "配置截图", "其他截图")
_BACKGROUND_COLORS = ("白色", "黑色", "灰色", "彩色", "其他")

# Vision Prompt 名称（从 system_prompt 表热加载）
_KBD_VISION_PROMPT_NAME = "kbd_vision_v1"

# LLM 配置（从环境变量读取，与 classify.py 保持一致，统一使用 LLM_* 命名）
# 注意：VISION_MODEL 从未在 Helm 中注入，直接使用 LLM_DEFAULT_MODEL（ConfigMap 已注入）
_LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "").rstrip("/")
_LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
# 优先读取 VISION_MODEL，若未配置，则回退到已验证可用的 qwen3.7-plus
_LLM_VISION_MODEL = os.environ.get("VISION_MODEL", "qwen3.7-plus")
_LLM_TIMEOUT = float(os.environ.get("LLM_TIMEOUT", "60.0"))
_VISION_MAX_TOKENS = int(os.environ.get("VISION_MAX_TOKENS", "8192"))


# ──────────────────────────────────────────────────────────────────────────────
# Step 0：文档解析与上下文提取
# ──────────────────────────────────────────────────────────────────────────────

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


# ──────────────────────────────────────────────────────────────────────────────
# 图片压缩（避免大图超时）
# ──────────────────────────────────────────────────────────────────────────────

def _compress_image_if_needed(image_data: bytes, mime_type: str) -> tuple[bytes, str]:
    """超过 1MB 时压缩图片。返回 (bytes, mime_type)。"""
    if len(image_data) <= _MAX_VISION_IMAGE_SIZE:
        return image_data, mime_type

    try:
        from PIL import Image
    except ImportError:
        logger.warning("Pillow 未安装，使用原图（大小=%dKB）", len(image_data) // 1024)
        return image_data, mime_type

    try:
        img = Image.open(io.BytesIO(image_data))
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        if img.width > 2000:
            ratio = 2000 / img.width
            img = img.resize((2000, int(img.height * ratio)), Image.Resampling.LANCZOS)
        buffer = io.BytesIO()
        img.save(buffer, format="JPEG", quality=85)
        compressed = buffer.getvalue()
        logger.info(
            "图片压缩 %dKB->%dKB",
            len(image_data) // 1024,
            len(compressed) // 1024,
        )
        return compressed, "image/jpeg"
    except Exception as exc:
        logger.warning("压缩失败：%s，使用原图", exc)
        return image_data, mime_type


# ──────────────────────────────────────────────────────────────────────────────
# Step 1：Vision LLM 单次调用
# ──────────────────────────────────────────────────────────────────────────────

async def _vision_analyze(
    client: AsyncOpenAI,
    image_data: bytes,
    mime_type: str,
    context: str,
    prompt_template: str,
) -> tuple[str, str, list[str], str]:
    """
    Vision LLM 单次调用，输出 TYPE + BACKGROUND + FULL_TEXT + DESCRIPTION。

    Returns:
        (type, background, full_text_lines, description)
        失败时返回 ("其他截图", "其他", [], "")
    """
    image_data, mime_type = _compress_image_if_needed(image_data, mime_type)
    b64 = base64.b64encode(image_data).decode("utf-8")
    data_uri = f"data:{mime_type};base64,{b64}"
    prompt = prompt_template.format(context=context or "（无上下文）")

    try:
        response = await client.chat.completions.create(
            model=_LLM_VISION_MODEL,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": data_uri}},
                ],
            }],
            max_tokens=_VISION_MAX_TOKENS,
            temperature=0.0,
            timeout=_LLM_TIMEOUT,
        )
        raw = (response.choices[0].message.content or "").strip()
        tokens = response.usage.total_tokens if response.usage else 0
        logger.debug("Vision LLM 响应 tokens=%d", tokens)
    except Exception as exc:
        logger.error("Vision LLM 失败：%s", exc)
        return "其他截图", "其他", [], ""

    screenshot_type = _parse_type(raw)
    background = _parse_background(raw)
    full_text = _parse_full_text(raw)
    description = _parse_description(raw)

    return screenshot_type, background, full_text, description


# ──────────────────────────────────────────────────────────────────────────────
# 输出解析（正则）
# ──────────────────────────────────────────────────────────────────────────────

def _parse_type(raw: str) -> str:
    m = re.search(r"TYPE[:\s]+(终端截图|日志截图|告警截图|任务截图|配置截图|其他截图)", raw)
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
    db_session: AsyncSession,
) -> dict[str, Any]:
    """重新识图单个 KBD 条目的所有图片。

    流程：
      1. 从 system_prompt 表热加载 Vision Prompt（kbd_vision_v1）
      2. 查询 kbd_image 表获取该 KBD 的所有原始图片
      3. 查询 kbd_entry 获取 content_md（用于提取图片上下文）
      4. 并发调用 Vision LLM（Semaphore 控制）
      5. 组装 images_json，更新 kbd_entry.images_json
      6. 调用 kbd_entry.rebuild_content_md() 重建 content_md
      7. 返回处理统计

    Args:
        kbd_entry_id: KBD 条目 ID
        db_session: SQLAlchemy AsyncSession

    Returns:
        {
            "kbd_entry_id": int,
            "total": int,       # 图片总数
            "done": int,        # 成功数
            "failed": int,      # 失败数
            "images_json": list,  # 新的 images_json
        }
    """
    # 1. 加载 Vision Prompt
    prompt_template = await StrictPromptLoader.load_and_validate(
        db_session,
        _KBD_VISION_PROMPT_NAME,
        ["context"],
        consumer="kb-service.vision_processor",
    )

    # 2. 查询 KBD 条目
    entry_result = await db_session.execute(
        select(KbdEntry).where(KbdEntry.id == kbd_entry_id)
    )
    kbd_entry = entry_result.scalar_one_or_none()
    if kbd_entry is None:
        raise ValueError(f"KBD 条目 {kbd_entry_id} 不存在")

    # 3. 查询所有原始图片
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
            "images_json": [],
            "message": "该 KBD 无原始图片，无法重算识图",
        }

    # 4. 构建图片上下文映射（从 content_md 提取，若有 HTML 则用 HTML）
    #    注：content_md 是 Markdown，不含原始 HTML，上下文可能有限
    #    后续可考虑存储 raw_html 以提供更精确的上下文
    context_source = kbd_entry.content_md or ""
    context_map = _build_context_map_from_html(context_source)

    # 5. 创建 LLM 客户端
    if not _LLM_API_KEY:
        raise RuntimeError("API_KEY 未配置，无法调用 Vision LLM")

    client = AsyncOpenAI(
        api_key=_LLM_API_KEY,
        base_url=_LLM_BASE_URL,
        timeout=_LLM_TIMEOUT,
    )

    # 6. 并发处理所有图片
    sem = asyncio.Semaphore(_VISION_CONCURRENCY)
    stats = {"done": 0, "failed": 0}
    images_json: list[dict[str, Any]] = []
    images_json_lock = asyncio.Lock()

    async def _process_one(kbd_image: KbdImage) -> None:
        seq = kbd_image.seq
        context = context_map.get(seq, "")

        async with sem:
            try:
                mime_type = kbd_image.mime_type or "image/png"
                screenshot_type, background, full_text, description = await _vision_analyze(
                    client,
                    kbd_image.image_data,
                    mime_type,
                    context,
                    prompt_template,
                )

                # 空结果重试一次
                if not full_text and not description:
                    logger.warning(
                        event="vision_empty_result_retry",
                        kbd_entry_id=kbd_entry_id,
                        seq=seq,
                    )
                    screenshot_type, background, full_text, description = await _vision_analyze(
                        client,
                        kbd_image.image_data,
                        mime_type,
                        context,
                        prompt_template,
                    )

                desc = _format_desc(screenshot_type, background, full_text, description)

                # 推断图片所属章节（简化：默认 steps_text，后续可从 content_md 解析）
                section = "steps_text"

                async with images_json_lock:
                    images_json.append({
                        "seq": seq,
                        "section": section,
                        "desc": desc,
                    })

                stats["done"] += 1
                logger.info(
                    event="vision_image_done",
                    kbd_entry_id=kbd_entry_id,
                    seq=seq,
                    screenshot_type=screenshot_type,
                )
            except Exception as exc:
                stats["failed"] += 1
                logger.error(
                    event="vision_image_failed",
                    kbd_entry_id=kbd_entry_id,
                    seq=seq,
                    error=str(exc),
                )

    await asyncio.gather(*[_process_one(img) for img in kbd_images])

    # 7. 按 seq 排序
    images_json.sort(key=lambda x: x["seq"])

    # 8. 更新 kbd_entry
    kbd_entry.images_json = images_json
    kbd_entry.content_md = kbd_entry.rebuild_content_md()

    await db_session.commit()

    logger.info(
        event="reanalyze_kbd_images_completed",
        kbd_entry_id=kbd_entry_id,
        total=len(kbd_images),
        done=stats["done"],
        failed=stats["failed"],
    )

    return {
        "kbd_entry_id": kbd_entry_id,
        "total": len(kbd_images),
        "done": stats["done"],
        "failed": stats["failed"],
        "images_json": images_json,
    }
