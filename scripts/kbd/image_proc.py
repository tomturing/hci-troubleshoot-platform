"""
scripts/kbd/image_proc.py — 图片语义化（Vision LLM 单次调用 + 文档上下文注入）

流水线（v3）：
  Step 0  解析文档 HTML，提取每张图片的上下文文字（纯 Python，无 LLM）
  Step 1  Pillow 背景色采样 → BACKGROUND（本地，无网络）
  Step 2  Vision LLM 单次调用（图片 + context + Prompt v3）
            → FULL_TEXT（文字原文照录，供人工审核）
            → DESCRIPTION（结合上下文的语义段落，供 RAG 召回）
  Step 3  规则引擎 → TYPE（本地，基于 FULL_TEXT + BACKGROUND，无 LLM）
  Step 4  组装 desc.txt v3 写入文件系统（幂等：已存在则跳过）

desc.txt v3 格式：
  BACKGROUND: 白色
  TYPE: 任务截图
  FULL_TEXT:
  - 失败
  - HA恢复虚拟机
  - ...（全量，不截断）
  DESCRIPTION:
  该截图为任务列表，展示了...（语义段落）

设计原则：
  - LLM 调用从 2 次/图 减少到 1 次/图
  - FULL_TEXT 全量存储，截断只在前端展示层按 TYPE + 阈值完成
  - TYPE 由本地规则引擎判断，不消耗 LLM token
  - 失败时写 img_N.desc.failed，不中断整体流程
  - 幂等：img_N.desc.txt 已存在则跳过
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import mimetypes
import re
from pathlib import Path
from typing import Any

from openai import AsyncOpenAI

from .config import settings
from .html_utils import extract_image_urls_with_positions as _extract_image_urls_with_positions

logger = logging.getLogger("kbd.image_proc")

# ──────────────────────────────────────────────────────────────────────────────
# 上下文提取常量
# ──────────────────────────────────────────────────────────────────────────────

# 低于此字数时向上扩展取文
_MIN_CONTEXT_CHARS = 80
# 优先取图片前净文字字数
_SHORT_WINDOW = 300
# 不足时扩展到此字数
_LONG_WINDOW = 800


# ──────────────────────────────────────────────────────────────────────────────
# Step 0：文档解析与上下文提取
# ──────────────────────────────────────────────────────────────────────────────

def _strip_html(html: str) -> str:
    """去除 HTML 标签、&nbsp;、多余空白，返回净文字。"""
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _extract_context(html: str, img_pos: int) -> str:
    """
    提取图片上文，优先短窗口（300字），不足则扩展到长窗口（800字）。

    Args:
        html:    原始 HTML 字符串
        img_pos: 图片标签在 HTML 中的字符位置

    Returns:
        净文字上下文（不含 HTML 标签）
    """
    short_raw = html[max(0, img_pos - _SHORT_WINDOW):img_pos]
    short_text = _strip_html(short_raw).strip()
    if len(short_text) >= _MIN_CONTEXT_CHARS:
        return short_text[-_SHORT_WINDOW:]

    long_raw = html[max(0, img_pos - _LONG_WINDOW):img_pos]
    long_text = _strip_html(long_raw).strip()
    return long_text[-_LONG_WINDOW:]


def build_context_map(html: str, base_url: str) -> dict[int, str]:
    """
    解析文档 HTML，返回 {图片序号: 上下文文字} 的映射。

    图片序号与 fetcher._extract_image_urls() 去重保序逻辑一致，
    因此与 cache 目录里的 img_N 文件名对应。

    Returns:
        {0: "上下文1", 1: "上下文2", ...}
    """
    img_positions = _extract_image_urls_with_positions(html, base_url)
    return {seq: _extract_context(html, pos) for seq, (_, pos) in enumerate(img_positions)}


# ──────────────────────────────────────────────────────────────────────────────
# Step 1：背景色检测（Pillow）
# ──────────────────────────────────────────────────────────────────────────────

def _detect_background(image_path: Path) -> str:
    """通过采样四角像素判断背景颜色，返回 黑色/白色/其他。"""
    try:
        from PIL import Image  # type: ignore[import-untyped]
    except ImportError:
        logger.warning("Pillow 未安装，背景色返回'其他'。安装：uv pip install pillow")
        return "其他"

    try:
        img = Image.open(image_path).convert("RGB")
        w, h = img.size
        sample_size = min(30, w // 4, h // 4)
        if sample_size < 5:
            return "其他"
        regions = [
            img.crop((0, 0, sample_size, sample_size)),
            img.crop((w - sample_size, 0, w, sample_size)),
            img.crop((0, h - sample_size, sample_size, h)),
            img.crop((w - sample_size, h - sample_size, w, h)),
        ]
        total_pixels = 0
        total_brightness = 0
        for region in regions:
            for r, g, b in region.getdata():  # type: ignore[misc]
                total_brightness += (r + g + b) / 3
                total_pixels += 1
        if total_pixels == 0:
            return "其他"
        avg = total_brightness / total_pixels
        if avg < 80:
            return "黑色"
        elif avg > 200:
            return "白色"
        return "其他"
    except Exception as exc:
        logger.warning("背景色检测失败 path=%s 原因=%s", image_path.name, exc)
        return "其他"


# ──────────────────────────────────────────────────────────────────────────────
# 图片压缩预处理（避免大图片超时）
# ──────────────────────────────────────────────────────────────────────────────

_MAX_VISION_IMAGE_SIZE = 500 * 1024  # 500KB，超过此大小需要压缩


def _compress_image_if_needed(image_path: Path) -> tuple[bytes, str]:
    """
    如果图片超过 MAX_VISION_IMAGE_SIZE，压缩到合适大小。

    Returns:
        (image_bytes, mime_type)
    """
    try:
        from PIL import Image  # type: ignore[import-untyped]
    except ImportError:
        logger.warning("Pillow 未安装，无法压缩图片，直接使用原图")
        return image_path.read_bytes(), mimetypes.guess_type(str(image_path))[0] or "image/png"

    original_size = image_path.stat().st_size
    mime_type = mimetypes.guess_type(str(image_path))[0] or "image/png"

    if original_size <= _MAX_VISION_IMAGE_SIZE:
        logger.debug("图片大小合适，无需压缩 path=%s size=%dKB", image_path.name, original_size // 1024)
        return image_path.read_bytes(), mime_type

    logger.info(
        "图片过大，开始压缩 path=%s original_size=%dKB max_size=%dKB",
        image_path.name,
        original_size // 1024,
        _MAX_VISION_IMAGE_SIZE // 1024,
    )

    try:
        img = Image.open(image_path)

        # 转换为 RGB（去除 alpha 通道以减小大小）
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")

        # 缩放图片：保持宽高比，宽度限制为 2000px
        max_width = 2000
        if img.width > max_width:
            ratio = max_width / img.width
            new_height = int(img.height * ratio)
            img = img.resize((max_width, new_height), Image.Resampling.LANCZOS)
            logger.debug(
                "图片缩放完成 path=%s 从 (%d, %d) 到 (%d, %d)",
                image_path.name,
                img.width,
                img.height,
                max_width,
                new_height,
            )

        # 保存为 JPEG（比 PNG 更小）
        import io
        buffer = io.BytesIO()
        quality = 85
        img.save(buffer, format="JPEG", quality=quality)
        compressed_data = buffer.getvalue()

        compressed_size = len(compressed_data)
        compression_ratio = (1 - compressed_size / original_size) * 100
        logger.info(
            "图片压缩完成 path=%s original=%dKB compressed=%dKB ratio=%.1f%%",
            image_path.name,
            original_size // 1024,
            compressed_size // 1024,
            compression_ratio,
        )

        return compressed_data, "image/jpeg"
    except Exception as exc:
        logger.warning("图片压缩失败 path=%s 原因=%s，使用原图", image_path.name, exc)
        return image_path.read_bytes(), mime_type


# ──────────────────────────────────────────────────────────────────────────────
# Step 2：Vision LLM 单次调用（文字照录 + 语义描述）
# ──────────────────────────────────────────────────────────────────────────────

_VISION_PROMPT_V3 = """\
你是HCI超融合平台故障排查文档助手。

这张截图出现在一篇故障排查案例文档中，截图前的文档内容如下：

【文档上下文】
{context}
【上下文结束】

请完成以下两个任务，严格按格式输出，禁止输出其他任何内容：

FULL_TEXT:
[将截图中所有可见文字原文照录，每行一个"- "条目]
[日志/终端截图：每条日志行单独一条，保留时间戳、级别、完整错误信息]
[任务列表截图：每行保留状态（失败/完成/进行中）、任务名、时间，每个字段单独一条]
[若截图中完全没有文字：- （无文字）]

DESCRIPTION:
[结合上方文档上下文，用2-4句技术语言描述：
  ① 这张截图展示了什么内容（是什么）
  ② 它与上下文中描述的故障现象有何关联（说明什么）
  ③ 截图揭示了什么问题、状态或结论（得出什么）
 不要复述上下文原文；用截图信息来解释和印证上下文；输出为连续段落，不要用列表格式]
"""


async def _vision_analyze(
    client: AsyncOpenAI,
    image_path: Path,
    mime_type: str,
    context: str,
) -> tuple[list[str], str]:
    """
    单次 Vision LLM 调用，同时输出 FULL_TEXT 和 DESCRIPTION。
    图片超过 500KB 时自动压缩以避免超时。

    Returns:
        (full_text_lines, description_paragraph)
        失败时返回 ([], "")
    """
    # 压缩预处理（大图片需要压缩以避免超时）
    image_data, actual_mime = _compress_image_if_needed(image_path)
    b64 = base64.b64encode(image_data).decode("utf-8")
    data_uri = f"data:{actual_mime};base64,{b64}"
    prompt = _VISION_PROMPT_V3.format(context=context or "（无上下文）")

    logger.info(
        "Vision LLM 开始 path=%s size=%dKB model=%s timeout=%ds",
        image_path.name, len(image_data) // 1024, settings.VISION_MODEL, settings.LLM_TIMEOUT,
    )

    try:
        response = await client.chat.completions.create(
            model=settings.VISION_MODEL,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": data_uri}},
                    ],
                }
            ],
            max_tokens=settings.VISION_MAX_TOKENS,
            temperature=0.0,
            timeout=settings.LLM_TIMEOUT,
        )
        # 详细日志：响应信息
        tokens = response.usage.total_tokens if response.usage else 0
        logger.debug(
            "Vision LLM 分析成功 path=%s tokens=%d finish_reason=%s",
            image_path.name,
            tokens,
            response.choices[0].finish_reason if response.choices else "N/A",
        )
    except Exception as exc:
        exc_type = type(exc).__name__
        if "Timeout" in exc_type or "timeout" in str(exc).lower():
            logger.error(
                "Vision LLM 超时 path=%s size=%dKB timeout=%ds 原因=%s",
                image_path.name, len(image_data) // 1024, settings.LLM_TIMEOUT, exc,
            )
        else:
            logger.error("Vision LLM 失败 path=%s 原因=%s: %s", image_path.name, exc_type, exc)
        return [], ""

    raw = (response.choices[0].message.content or "").strip()
    logger.debug("Vision LLM 原始输出（前300字）：%s", raw[:300])

    full_text = _parse_full_text(raw)
    description = _parse_description(raw)

    tokens = response.usage.total_tokens if response.usage else 0
    logger.info("Vision LLM 完成 path=%s lines=%d tokens=%d", image_path.name, len(full_text), tokens)
    return full_text, description


_RE_FULL_TEXT_SECTION = re.compile(r"FULL_TEXT:\s*\n((?:^-\s.+\n?)+)", re.MULTILINE)
_RE_DESCRIPTION_SECTION = re.compile(r"DESCRIPTION:\s*\n(.+?)(?=\n[A-Z_]+:|$)", re.MULTILINE | re.DOTALL)
_RE_BULLET = re.compile(r"^-\s+(.+)$", re.MULTILINE)


def _parse_full_text(raw: str) -> list[str]:
    """从 LLM 输出中解析 FULL_TEXT section 的 bullet 行。"""
    m = _RE_FULL_TEXT_SECTION.search(raw)
    if m:
        items = _RE_BULLET.findall(m.group(1))
    else:
        ft_start = raw.find("FULL_TEXT:")
        desc_start = raw.find("DESCRIPTION:")
        if ft_start == -1:
            return []
        end = desc_start if desc_start > ft_start else len(raw)
        items = _RE_BULLET.findall(raw[ft_start:end])

    lines = [item.strip() for item in items if item.strip()]
    if lines in (["（无文字）"], ["(无文字)"]):
        return []
    return lines


def _parse_description(raw: str) -> str:
    """从 LLM 输出中解析 DESCRIPTION section 的段落文字。"""
    m = _RE_DESCRIPTION_SECTION.search(raw)
    if m:
        return m.group(1).strip()
    desc_start = raw.find("DESCRIPTION:")
    if desc_start == -1:
        return ""
    return raw[desc_start + len("DESCRIPTION:"):].strip()


# ──────────────────────────────────────────────────────────────────────────────
# Step 3：TYPE 规则引擎（本地，无 LLM）
# ──────────────────────────────────────────────────────────────────────────────

def classify_type(background: str, full_text: list[str]) -> str:
    """
    基于背景色 + 文字内容，用正则规则本地判断截图类型。

    Returns:
        "终端截图" | "日志截图" | "告警截图" | "任务截图" | "其他截图"
    """
    text = " ".join(full_text)

    if background == "黑色":
        if re.search(r"\$\s|#\s|sudo |grep |chmod |cat |ls |\-rn |sfvt_", text):
            return "终端截图"
        if re.search(r"\d{4}-\d{2}-\d{2}[\sT]\d{2}:\d{2}:\d{2}", text) or re.search(
            r"\b(ERROR|WARN|error|warn|INFO|DEBUG|FATAL)\b", text
        ):
            return "日志截图"
        return "终端截图"

    if background == "白色":
        if re.search(r"紧急|严重|告警|未处理|已触发", text):
            return "告警截图"
        if re.search(r"失败|完成|进行中|操作人|HA恢复|修复.*快照|新建.*快照", text):
            return "任务截图"

    return "其他截图"


# ──────────────────────────────────────────────────────────────────────────────
# Step 4：desc.txt v3 组装
# ──────────────────────────────────────────────────────────────────────────────

def _format_desc_v3(
    background: str,
    screenshot_type: str,
    full_text: list[str],
    description: str,
) -> str:
    """组装 v3 格式 desc.txt 字符串。"""
    lines = [
        f"BACKGROUND: {background}",
        f"TYPE: {screenshot_type}",
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
# 核心处理函数
# ──────────────────────────────────────────────────────────────────────────────

async def _process_image(
    client: AsyncOpenAI,
    image_path: Path,
    mime_type: str,
    context: str = "",
) -> str:
    """
    对单张图片执行完整 v3 流水线，返回 desc.txt 文本。

    Args:
        client:     AsyncOpenAI 客户端
        image_path: 图片路径
        mime_type:  图片 MIME 类型
        context:    从文档 HTML 提取的上下文净文字
    """
    # Step 1: 背景色（本地）
    background = await asyncio.to_thread(_detect_background, image_path)
    logger.debug("背景色 path=%s 结果=%s", image_path.name, background)

    # Step 2: Vision LLM 单次调用
    full_text, description = await _vision_analyze(client, image_path, mime_type, context)

    # Step 3: 规则引擎分类
    screenshot_type = classify_type(background, full_text)

    # Step 4: 组装 v3
    return _format_desc_v3(background, screenshot_type, full_text, description)


def _find_images(case_dir: Path) -> list[Path]:
    """扫描案例缓存目录，返回图片列表（按 seq 排序）。"""
    img_suffixes = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
    images: list[Path] = []
    for p in case_dir.iterdir():
        if p.name.startswith("img_") and p.suffix.lower() in img_suffixes:
            images.append(p)

    def _seq(p: Path) -> int:
        try:
            return int(p.stem.split("_", 1)[1])
        except (IndexError, ValueError):
            return 0

    images.sort(key=_seq)
    return images


async def process_images_for_case(
    case_id: str,
    client: AsyncOpenAI,
) -> dict[str, int]:
    """
    处理单个案例的所有图片，将 v3 格式描述写入 img_N.desc.txt。

    Returns:
        {"done": N, "failed": N, "skipped": N}
    """
    from .fetcher import _case_dir
    case_dir = _case_dir(case_id)
    stats: dict[str, int] = {"done": 0, "failed": 0, "skipped": 0}

    images = _find_images(case_dir)
    if not images:
        return stats

    # Step 0: 解析文档，建立图片序号 → 上下文的映射
    context_map = _load_context_map(case_dir)
    logger.info(
        "上下文映射构建完成 case_id=%s images=%d contexts=%d",
        case_id, len(images), len(context_map),
    )

    sem = asyncio.Semaphore(settings.VISION_CONCURRENCY)

    async def _process_one(img_path: Path) -> None:
        desc_path = img_path.with_suffix(".desc.txt")
        if desc_path.exists():
            stats["skipped"] += 1
            return

        try:
            seq = int(img_path.stem.split("_", 1)[1])
        except (IndexError, ValueError):
            seq = -1
        context = context_map.get(seq, "")

        mime = mimetypes.guess_type(str(img_path))[0] or "image/jpeg"
        async with sem:
            try:
                desc = await _process_image(client, img_path, mime, context)
                desc_path.write_text(desc, encoding="utf-8")
                stats["done"] += 1
                type_line = next((ln for ln in desc.split("\n") if ln.startswith("TYPE:")), "TYPE: ?")
                logger.info("图片处理完成 path=%s %s", img_path.name, type_line)
            except Exception as exc:
                logger.error("图片处理失败 path=%s 原因=%s", img_path.name, exc)
                img_path.with_suffix(".desc.failed").write_text(str(exc), encoding="utf-8")
                stats["failed"] += 1

    await asyncio.gather(*[_process_one(img) for img in images])
    return stats


def _load_context_map(case_dir: Path) -> dict[int, str]:
    """
    从 raw.json 解析文档 HTML，构建 {图片序号: 上下文文字} 映射。
    raw.json 不存在时返回空字典（所有图片上下文为空）。
    """
    raw_path = case_dir / "raw.json"
    if not raw_path.exists():
        logger.warning("raw.json 不存在，图片上下文为空 case_dir=%s", case_dir)
        return {}

    with raw_path.open(encoding="utf-8") as f:
        data = json.load(f)

    html = data.get("content") or data.get("contentWeb") or ""
    if not html:
        logger.warning("raw.json 中 content 字段为空 case_dir=%s", case_dir)
        return {}

    base_url = settings.SANGFOR_API_BASE
    try:
        return build_context_map(html, base_url)
    except Exception as exc:
        logger.warning("构建上下文映射失败 case_dir=%s 原因=%s", case_dir, exc)
        return {}


async def process_images_batch(
    case_ids: list[str],
    _pool: Any = None,
) -> dict[str, int]:
    """批量处理一组案例的图片（保留旧接口签名以兼容调用方）。"""
    from .fetcher import _case_dir as _cd

    client = AsyncOpenAI(
        api_key=settings.ZAI_API_KEY,
        base_url=settings.ZAI_BASE_URL,
        timeout=settings.LLM_TIMEOUT,
    )
    total_stats: dict[str, int] = {"done": 0, "failed": 0, "skipped": 0}
    total = len(case_ids)

    for idx, case_id in enumerate(case_ids, 1):
        case_dir = _cd(case_id)
        pending = [
            p for p in _find_images(case_dir)
            if not p.with_suffix(".desc.txt").exists()
        ]
        if not pending:
            logger.debug("[%d/%d] 案例 %s 无待处理图片，跳过", idx, total, case_id)
            continue

        logger.info("[%d/%d] 处理案例 %s 共 %d 张图片", idx, total, case_id, len(pending))
        stats = await process_images_for_case(case_id, client)
        for k in total_stats:
            total_stats[k] += stats.get(k, 0)

    logger.info(
        "图片处理完成 done=%d failed=%d skipped=%d",
        total_stats["done"], total_stats["failed"], total_stats["skipped"],
    )
    return total_stats
