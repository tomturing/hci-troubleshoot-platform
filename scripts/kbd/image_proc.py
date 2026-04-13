"""
scripts/kbd/image_proc.py — 图片语义化（Vision LLM + 分析 LLM 双模型）

流水线：
  1. Vision LLM（qwen3.5-plus）→ 文字照录 OCR（只输出原文，不分析）
  2. Pillow → 背景色采样（黑色 / 白色 / 其他）
  3. qwen3-max LLM → 类型判断 + KEY 提取 + TIPS 生成（纯文本，无图片）
  4. 组装 v2 格式 desc.txt 写入文件系统

desc.txt v2 格式：
  BACKGROUND: 黑色
  TYPE: 日志截图
  FULL_TEXT:
  - 第1行
  KEY:
  - ERROR日志行
  TIPS:
  - 排障建议

设计：
  - 纯文件系统操作，无数据库依赖
  - 并发处理（asyncio.Semaphore 控制并发数）
  - 失败时写 img_N.desc.failed，不中断整体流程
  - 幂等：img_N.desc.txt 已存在则跳过
"""
from __future__ import annotations

import asyncio
import base64
import logging
import mimetypes
from pathlib import Path
from typing import Any

from openai import AsyncOpenAI

from .analyzer import AnalysisResult, analyze_screenshot
from .config import settings

logger = logging.getLogger("kbd.image_proc")


# ──────────────────────────────────────────────────────────────────────────────
# 背景色检测
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
# Vision LLM 兜底 OCR
# ──────────────────────────────────────────────────────────────────────────────

_VISION_OCR_PROMPT = """\
这是一张 HCI 超融合平台的故障排查截图。
请将截图中所有可见的文字内容原文照录，每行文字单独一行，不要添加任何解释或分析。
只输出文字内容本身，不要使用 Markdown、JSON 或其他格式。
如果截图中没有文字，输出：（无文字）
"""


async def _vision_ocr_fallback(
    client: AsyncOpenAI,
    image_path: Path,
    mime_type: str,
) -> list[str]:
    """Vision LLM 兜底 OCR，返回文字行列表；失败时返回空列表。"""
    image_data = image_path.read_bytes()
    b64 = base64.b64encode(image_data).decode("utf-8")
    data_uri = f"data:{mime_type};base64,{b64}"

    try:
        response = await client.chat.completions.create(
            model=settings.VISION_MODEL,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": _VISION_OCR_PROMPT},
                        {"type": "image_url", "image_url": {"url": data_uri, "detail": "high"}},
                    ],
                }
            ],
            max_tokens=2048,
            temperature=0.0,
            timeout=settings.LLM_TIMEOUT,
        )
    except Exception as exc:
        logger.error("Vision 兜底 OCR 失败 path=%s 原因=%s", image_path.name, exc)
        return []

    raw = (response.choices[0].message.content or "").strip()
    if raw in ("（无文字）", "(无文字)", ""):
        return []
    lines = [line.strip() for line in raw.split("\n") if line.strip()]
    logger.info("Vision 兜底 OCR 完成 path=%s 行数=%d", image_path.name, len(lines))
    return lines


# ──────────────────────────────────────────────────────────────────────────────
# desc.txt 组装（v2 格式）
# ──────────────────────────────────────────────────────────────────────────────

def _format_desc_v2(
    background: str,
    result: AnalysisResult,
    full_text: list[str],
) -> str:
    """组装 v2 格式 desc.txt 字符串。"""
    lines = [
        f"BACKGROUND: {background}",
        f"TYPE: {result.type}",
        "FULL_TEXT:",
    ]
    for line in full_text:
        lines.append(f"- {line}")
    if not full_text:
        lines.append("- （无文字）")

    lines.append("KEY:")
    if result.key:
        for item in result.key:
            lines.append(f"- {item}")
    else:
        lines.append("- 无")

    lines.append("TIPS:")
    if result.tips:
        for tip in result.tips:
            lines.append(f"- {tip}")
    else:
        lines.append("- 无")

    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────────────
# 核心处理函数
# ──────────────────────────────────────────────────────────────────────────────

async def _process_image(
    client: AsyncOpenAI,
    image_path: Path,
    mime_type: str,
) -> tuple[str, int]:
    """对单张图片执行完整双模型处理流水线，返回 (desc_text, 0)。"""
    # Step 1：Vision LLM 文字照录（qwen3.5-plus，只提取文字不分析）
    full_text: list[str] = await _vision_ocr_fallback(client, image_path, mime_type)

    # Step 2：Pillow 背景色采样
    background = await asyncio.to_thread(_detect_background, image_path)
    logger.debug("背景色 path=%s 结果=%s", image_path.name, background)

    # Step 3：LLM 分析
    result = await analyze_screenshot(
        background=background,
        full_text=full_text,
        client=client,
        model=settings.ANALYSIS_MODEL,
        timeout=settings.LLM_TIMEOUT,
    )

    # Step 4：组装 v2 desc
    desc = _format_desc_v2(background, result, full_text)
    return desc, 0


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
    处理单个案例的所有图片，将 v2 格式描述写入 img_N.desc.txt。

    Returns:
        {"done": N, "failed": N, "skipped": N}
    """
    from .fetcher import _case_dir
    case_dir = _case_dir(case_id)
    stats: dict[str, int] = {"done": 0, "failed": 0, "skipped": 0}

    images = _find_images(case_dir)
    if not images:
        return stats

    sem = asyncio.Semaphore(settings.VISION_CONCURRENCY)

    async def _process_one(img_path: Path) -> None:
        desc_path = img_path.with_suffix(".desc.txt")
        if desc_path.exists():
            stats["skipped"] += 1
            return

        mime = mimetypes.guess_type(str(img_path))[0] or "image/jpeg"
        async with sem:
            try:
                desc, _ = await _process_image(client, img_path, mime)
                desc_path.write_text(desc, encoding="utf-8")
                stats["done"] += 1
                type_line = next((l for l in desc.split("\n") if l.startswith("TYPE:")), "TYPE: ?")
                logger.info("图片处理完成 path=%s %s", img_path.name, type_line)
            except Exception as exc:
                logger.error("图片处理失败 path=%s 原因=%s", img_path.name, exc)
                (img_path.with_suffix(".desc.failed")).write_text(str(exc), encoding="utf-8")
                stats["failed"] += 1

    await asyncio.gather(*[_process_one(img) for img in images])
    return stats


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
