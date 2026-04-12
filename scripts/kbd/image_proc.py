"""
scripts/kbd/image_proc.py — 图片语义化（Vision LLM）

功能：
  扫描 cache/{support_id}/ 目录下的图片文件（img_N.*），
  对尚无对应 img_N.desc.txt 的图片调用 Vision LLM 生成中文语义描述，
  将描述写入 img_N.desc.txt 供 converter.py 使用。

描述重点：
  - 界面类型（命令行 / Web 管理界面 / 错误弹窗 / 日志等）
  - 关键数字、状态值、错误码、错误信息
  - 指向故障根因的技术信息

设计：
  - 纯文件系统操作，无数据库依赖
  - 读本地图片文件，以 Base64 + data URI 发送（避免 URL 访问权限问题）
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

from .config import settings

logger = logging.getLogger("kbd.image_proc")

# Vision prompt —— 专门针对 HCI 故障排查截图
# 字段 0（背景色）用于前端截图自动分类：白色=管理界面，黑色=终端/日志
_VISION_PROMPT = """\
这是一张深信服 HCI 超融合平台的故障排查截图，请用中文描述。

严格按以下格式输出，禁止使用 JSON、Markdown 标题（#）、表格，每行必须完全符合示例格式：

0. **截图背景颜色**：黑色
1. **截图界面类型**：命令行终端
2. **截图中所有可见的文字内容**：
- 第一行关键文字原文
- 第二行关键文字原文
3. **截图中的报错信息或异常状态**：
- 报错原文（无报错写：无）
4. **对故障排查有价值的技术细节**：
- 关键技术细节

注意：字段0只填黑色/白色/其他；字段2和3内容原文照录不加工，每项一行bullet（"- "开头），最多6条；总输出不超过300字。
"""


import json as _json
import re as _re


def _normalize_vision_output(text: str) -> str:
    """
    将 Vision LLM 可能输出的 JSON 格式归一化为编号字段格式。
    如果不是 JSON 则原样返回。
    """
    stripped = text.strip()
    # 尝试提取 JSON 代码块
    json_match = _re.search(r"```(?:json)?\s*(\{.*?\})\s*```", stripped, _re.DOTALL)
    if json_match:
        json_str = json_match.group(1)
    elif stripped.startswith("{"):
        json_str = stripped
    else:
        return text

    try:
        data = _json.loads(json_str)
    except (_json.JSONDecodeError, ValueError):
        return text

    bg = data.get("截图背景颜色", "其他")
    iface = data.get("截图界面类型", "")
    visible = data.get("截图中所有可见的文字内容", [])
    errors = data.get("截图中的报错信息或异常状态", [])
    tips = data.get("对故障排查有价值的技术细节", [])

    # 统一转为列表
    def _to_list(v: object) -> list[str]:
        if isinstance(v, list):
            return [str(i) for i in v]
        if v and str(v) not in ("无", ""):
            return [str(v)]
        return []

    visible_list = _to_list(visible)
    errors_list = _to_list(errors)
    tips_list = _to_list(tips)

    error_bullets = [f"- {item}" for item in errors_list[:3]] if errors_list else ["- 无"]
    lines = [
        f"0. **截图背景颜色**：{bg}",
        f"1. **截图界面类型**：{iface}",
        "2. **截图中所有可见的文字内容**：",
        *[f"- {item}" for item in visible_list[:6]],
        "3. **截图中的报错信息或异常状态**：",
        *error_bullets,
        "4. **对故障排查有价值的技术细节**：",
        *[f"- {item}" for item in tips_list[:3]],
    ]
    return "\n".join(lines)



async def _describe_image(
    client: AsyncOpenAI,
    image_path: Path,
    mime_type: str,
) -> tuple[str, int]:
    """
    对单张本地图片调用 Vision LLM，返回 (描述文本, token数)。
    """
    # 读取文件并编码为 Base64 data URI
    image_data = image_path.read_bytes()
    b64 = base64.b64encode(image_data).decode("utf-8")
    data_uri = f"data:{mime_type};base64,{b64}"

    response = await client.chat.completions.create(
        model=settings.VISION_MODEL,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": _VISION_PROMPT},
                    {"type": "image_url", "image_url": {"url": data_uri, "detail": "high"}},
                ],
            }
        ],
        max_tokens=400,
        temperature=0.1,
        timeout=settings.LLM_TIMEOUT,
    )
    desc = response.choices[0].message.content or ""
    tokens = response.usage.total_tokens if response.usage else 0
    # 归一化输出格式（防止 Vision 模型返回 JSON 格式）
    desc = _normalize_vision_output(desc.strip())
    return desc, tokens


def _find_images(case_dir: Path) -> list[Path]:
    """
    扫描案例缓存目录，返回待处理的图片文件列表（按 seq 排序）。
    排除 .failed、.txt 等非图片文件。
    """
    # 支持的后缀
    img_suffixes = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
    images: list[Path] = []
    for p in case_dir.iterdir():
        if p.name.startswith("img_") and p.suffix.lower() in img_suffixes:
            images.append(p)
    # 按 img_N 中的 N 排序
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
    处理单个案例的所有待处理图片，将描述写入 img_N.desc.txt。

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
        # 已有描述文件 → 跳过（幂等）
        if desc_path.exists():
            stats["skipped"] += 1
            return

        mime = mimetypes.guess_type(str(img_path))[0] or "image/jpeg"

        async with sem:
            try:
                desc, tokens = await _describe_image(client, img_path, mime)
                desc_path.write_text(desc, encoding="utf-8")
                stats["done"] += 1
                logger.debug("图片描述完成 path=%s tokens=%d", img_path.name, tokens)
            except Exception as exc:
                logger.error("图片描述失败 path=%s 原因=%s", img_path.name, exc)
                (img_path.with_suffix(".desc.failed")).write_text(
                    str(exc), encoding="utf-8"
                )
                stats["failed"] += 1

    await asyncio.gather(*[_process_one(img) for img in images])
    return stats


async def process_images_batch(
    case_ids: list[str],
    _pool: Any = None,  # 废弃参数，保留向后兼容
) -> dict[str, int]:
    """
    批量处理一组案例的图片语义化（纯文件系统版）。

    Args:
        case_ids: 要处理的案例 ID 列表
        _pool:    废弃参数（原 asyncpg 连接池），忽略

    Returns:
        汇总统计 {"done": N, "failed": N, "skipped": N}
    """
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
        "图片语义化完成 done=%d failed=%d skipped=%d",
        total_stats["done"], total_stats["failed"], total_stats["skipped"],
    )
    return total_stats
