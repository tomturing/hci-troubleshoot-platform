"""
data-pipeline/kbd/image_proc.py — 图片语义化（Vision LLM v5）

流水线（v5，彻底简化）：
  Step 0  解析文档 HTML，提取每张图片的上下文文字（纯 Python，无 LLM）
  Step 1  Vision LLM 单次调用（图片 + context + Prompt v5）
            → TYPE（截图类型：终端/日志/告警/任务/配置/其他）
            → BACKGROUND（背景颜色：白色/黑色/灰色/彩色/其他）
            → FULL_TEXT（文字原文照录）
            → DESCRIPTION（语义描述）
  Step 2  组装 desc.txt 写入文件系统（幂等）

设计原则：
  - 所有判断（TYPE + BACKGROUND）由 Vision LLM 完成，无需本地规则引擎
  - 代码极简：移除 Pillow 背景色检测、移除正则规则引擎
  - 单次 LLM 调用，效率最大化
  - 幂等：已存在则跳过
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
# 常量配置
# ──────────────────────────────────────────────────────────────────────────────

_MIN_CONTEXT_CHARS = 80   # 低于此字数时向上扩展
_SHORT_WINDOW = 300       # 优先取图片前净文字字数
_LONG_WINDOW = 800        # 不足时扩展到此字数
_MAX_VISION_IMAGE_SIZE = 1024 * 1024  # 1MB，超过需压缩（文字截图需要清晰度）

# 截图类型（LLM 输出标准）
SCREENSHOT_TYPES = ("终端截图", "日志截图", "告警截图", "任务截图", "配置截图", "其他截图")
# 背景颜色（LLM 输出标准）
BACKGROUND_COLORS = ("白色", "黑色", "灰色", "彩色", "其他")

# Prompt 文件路径（v5）
_PROMPT_PATH = Path(__file__).parent / "prompt" / "image_proc_vision_v4.txt"
if not _PROMPT_PATH.exists():
    raise RuntimeError(f"Vision Prompt 文件不存在: {_PROMPT_PATH}")
_PROMPT: str = _PROMPT_PATH.read_text(encoding="utf-8")


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


def build_context_map(html: str, base_url: str) -> dict[int, str]:
    """解析文档 HTML，返回 {图片序号: 上下文文字}。"""
    img_positions = _extract_image_urls_with_positions(html, base_url)
    return {seq: _extract_context(html, pos) for seq, (_, pos) in enumerate(img_positions)}


# ──────────────────────────────────────────────────────────────────────────────
# 图片压缩（避免大图超时）
# ──────────────────────────────────────────────────────────────────────────────

def _compress_image_if_needed(image_path: Path) -> tuple[bytes, str]:
    """超过 500KB 时压缩图片。返回 (bytes, mime_type)。"""
    try:
        from PIL import Image
    except ImportError:
        return image_path.read_bytes(), "image/png"

    original_size = image_path.stat().st_size
    mime_type = mimetypes.guess_type(str(image_path))[0] or "image/png"

    if original_size <= _MAX_VISION_IMAGE_SIZE:
        return image_path.read_bytes(), mime_type

    try:
        img = Image.open(image_path)
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        if img.width > 2000:
            ratio = 2000 / img.width
            img = img.resize((2000, int(img.height * ratio)), Image.Resampling.LANCZOS)
        import io
        buffer = io.BytesIO()
        img.save(buffer, format="JPEG", quality=85)
        logger.info("图片压缩 path=%s %dKB→%dKB", image_path.name, original_size // 1024, len(buffer.getvalue()) // 1024)
        return buffer.getvalue(), "image/jpeg"
    except Exception as exc:
        logger.warning("压缩失败 path=%s %s，使用原图", image_path.name, exc)
        return image_path.read_bytes(), mime_type


# ──────────────────────────────────────────────────────────────────────────────
# Step 1：Vision LLM 单次调用
# ──────────────────────────────────────────────────────────────────────────────

async def _vision_analyze(
    client: AsyncOpenAI,
    image_path: Path,
    context: str,
) -> tuple[str, str, list[str], str]:
    """
    Vision LLM 单次调用，输出 TYPE + BACKGROUND + FULL_TEXT + DESCRIPTION。

    Returns:
        (type, background, full_text_lines, description)
        失败时返回 ("其他截图", "其他", [], "")
    """
    image_data, mime_type = _compress_image_if_needed(image_path)
    b64 = base64.b64encode(image_data).decode("utf-8")
    data_uri = f"data:{mime_type};base64,{b64}"
    prompt = _PROMPT.format(context=context or "（无上下文）")

    logger.info("Vision LLM 开始 path=%s size=%dKB", image_path.name, len(image_data) // 1024)

    try:
        response = await client.chat.completions.create(
            model=settings.VISION_MODEL,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": data_uri}},
                ],
            }],
            max_tokens=settings.VISION_MAX_TOKENS,
            temperature=0.0,
            timeout=settings.LLM_TIMEOUT,
        )
        raw = (response.choices[0].message.content or "").strip()
        tokens = response.usage.total_tokens if response.usage else 0
        logger.debug("Vision LLM 响应 tokens=%d", tokens)
    except Exception as exc:
        logger.error("Vision LLM 失败 path=%s %s", image_path.name, exc)
        return "其他截图", "其他", [], ""

    # 解析四个字段
    screenshot_type = _parse_type(raw)
    background = _parse_background(raw)
    full_text = _parse_full_text(raw)
    description = _parse_description(raw)

    logger.info("Vision LLM 完成 path=%s type=%s bg=%s lines=%d", image_path.name, screenshot_type, background, len(full_text))
    return screenshot_type, background, full_text, description


# ──────────────────────────────────────────────────────────────────────────────
# 输出解析（正则）
# ──────────────────────────────────────────────────────────────────────────────

def _parse_type(raw: str) -> str:
    """解析 TYPE 字段。"""
    m = re.search(r"TYPE[:\s]+(终端截图|日志截图|告警截图|任务截图|配置截图|其他截图)", raw)
    if m:
        return m.group(1)
    return "其他截图"


def _parse_background(raw: str) -> str:
    """解析 BACKGROUND 字段。"""
    m = re.search(r"BACKGROUND[:\s]+(白色|黑色|灰色|彩色|其他)", raw)
    if m:
        return m.group(1)
    return "其他"


def _parse_full_text(raw: str) -> list[str]:
    """解析 FULL_TEXT section 的 bullet 行。"""
    # 找到 FULL_TEXT 区域
    ft_start = raw.find("FULL_TEXT:")
    if ft_start == -1:
        return []

    # 找到下一个分隔符或 DESCRIPTION
    next_section = raw.find("DESCRIPTION:", ft_start)
    if next_section == -1:
        next_section = raw.find("════════", ft_start)
    if next_section == -1:
        next_section = len(raw)

    ft_section = raw[ft_start:next_section]

    # 提取所有 bullet 行
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
    # 找到 DESCRIPTION 开始位置
    desc_start = raw.find("DESCRIPTION:")
    if desc_start == -1:
        return ""

    # 找到结束位置：下一个大写字母字段、装饰线或文件末尾
    rest = raw[desc_start + 12:]

    # 结束标志：下一行以 TYPE/BACKGROUND/FULL_TEXT/═══ 开头
    end_pos = len(rest)
    for pattern in ["\nTYPE:", "\nBACKGROUND:", "\nFULL_TEXT:", "\n═══"]:
        idx = rest.find(pattern)
        if idx != -1 and idx < end_pos:
            end_pos = idx

    desc = rest[:end_pos].strip()
    # 清理末尾装饰线
    desc = re.sub(r"═══.*$", "", desc).strip()
    return desc if desc and desc not in ("（无描述）", "(无描述)") else ""


# ──────────────────────────────────────────────────────────────────────────────
# Step 2：组装 desc.txt
# ──────────────────────────────────────────────────────────────────────────────

def _format_desc(screenshot_type: str, background: str, full_text: list[str], description: str) -> str:
    """组装 desc.txt 格式。"""
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
# 核心处理函数
# ──────────────────────────────────────────────────────────────────────────────

async def _process_image(client: AsyncOpenAI, image_path: Path, context: str = "") -> str:
    """处理单张图片，返回 desc.txt 内容。"""
    screenshot_type, background, full_text, description = await _vision_analyze(client, image_path, context)
    return _format_desc(screenshot_type, background, full_text, description)


def _find_images(kbd_dir: Path) -> list[Path]:
    """扫描案例缓存目录，返回图片列表（按序号排序）。"""
    img_suffixes = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
    images = [p for p in kbd_dir.iterdir() if p.name.startswith("img_") and p.suffix.lower() in img_suffixes]
    images.sort(key=lambda p: int(p.stem.split("_")[1]) if p.stem.split("_")[1].isdigit() else 0)
    return images


def _load_context_map(kbd_dir: Path) -> dict[int, str]:
    """从 raw.json 构建图片上下文映射。"""
    raw_path = kbd_dir / "raw.json"
    if not raw_path.exists():
        return {}
    try:
        data = json.loads(raw_path.read_text(encoding="utf-8"))
        html = data.get("content") or data.get("contentWeb") or ""
        if not html:
            return {}
        return build_context_map(html, settings.SANGFOR_API_BASE)
    except Exception as exc:
        logger.warning("构建上下文失败 kbd_dir=%s %s", kbd_dir, exc)
        return {}


async def process_images_for_kbd(kbd_id: str, client: AsyncOpenAI) -> dict[str, int]:
    """处理单个案例的所有图片。返回 {"done": N, "failed": N, "skipped": N}。"""
    from .fetcher import _kbd_dir
    kbd_dir = _kbd_dir(kbd_id)
    stats = {"done": 0, "failed": 0, "skipped": 0}

    images = _find_images(kbd_dir)
    if not images:
        return stats

    context_map = _load_context_map(kbd_dir)
    sem = asyncio.Semaphore(settings.VISION_CONCURRENCY)

    async def _process_one(img_path: Path):
        desc_path = img_path.with_suffix(".desc.txt")
        if desc_path.exists():
            stats["skipped"] += 1
            return

        seq = int(img_path.stem.split("_")[1]) if img_path.stem.split("_")[1].isdigit() else -1
        context = context_map.get(seq, "")

        async with sem:
            try:
                desc = await _process_image(client, img_path, context)
                desc_path.write_text(desc, encoding="utf-8")
                stats["done"] += 1
                logger.info("完成 path=%s TYPE=%s", img_path.name, desc.split("\n")[0])
            except Exception as exc:
                logger.error("失败 path=%s %s", img_path.name, exc)
                img_path.with_suffix(".desc.failed").write_text(str(exc), encoding="utf-8")
                stats["failed"] += 1

    await asyncio.gather(*[_process_one(img) for img in images])
    return stats


async def process_images_batch(kbd_ids: list[str], _pool: Any = None) -> dict[str, int]:
    """批量处理图片。"""
    from .fetcher import _kbd_dir as _cd

    client = AsyncOpenAI(api_key=settings.ZAI_API_KEY, base_url=settings.ZAI_BASE_URL, timeout=settings.LLM_TIMEOUT)
    total_stats = {"done": 0, "failed": 0, "skipped": 0}

    for idx, kbd_id in enumerate(kbd_ids, 1):
        kbd_dir = _cd(kbd_id)
        pending = [p for p in _find_images(kbd_dir) if not p.with_suffix(".desc.txt").exists()]
        if not pending:
            continue
        logger.info("[%d/%d] 处理 %s 共 %d 张", idx, len(kbd_ids), kbd_id, len(pending))
        stats = await process_images_for_kbd(kbd_id, client)
        for k in total_stats:
            total_stats[k] += stats[k]

    logger.info("批量完成 done=%d failed=%d skipped=%d", total_stats["done"], total_stats["failed"], total_stats["skipped"])
    return total_stats


def get_failed_vision_ids(kbd_ids: list[str]) -> list[str]:
    """筛选 Vision 处理失败的案例。"""
    from .fetcher import _kbd_dir
    failed = []
    for cid in kbd_ids:
        kbd_dir = _kbd_dir(cid)
        if _has_failed_vision(kbd_dir):
            failed.append(cid)
    return failed


def _has_failed_vision(kbd_dir: Path) -> bool:
    """检查是否有图片处理失败的标记。"""
    return any(kbd_dir.glob("img_*.desc.failed"))