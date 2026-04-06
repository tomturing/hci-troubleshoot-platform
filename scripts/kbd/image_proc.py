"""
scripts/kbd/image_proc.py — 图片语义化（Vision LLM）

功能：
  对 kbd_image 表中 vision_status='pending' 的图片，
  调用 Vision LLM 生成汉语语义描述，写回 vision_desc 字段。

描述重点：
  - 界面类型（命令行 / Web 管理界面 / 错误弹窗 / 日志等）
  - 关键数字、状态值、错误码、错误信息
  - 指向故障根因的技术信息

设计：
  - 读本地图片文件，以 Base64 + data URI 发送（避免 URL 访问权限问题）
  - 并发处理（asyncio.Semaphore 控制并发数）
  - 失败时标记 vision_status='failed'，不中断整体流程
"""
from __future__ import annotations

import asyncio
import base64
import logging
from pathlib import Path

import asyncpg
from openai import AsyncOpenAI

from .config import settings

logger = logging.getLogger("kbd.image_proc")

# Vision prompt —— 专门针对 HCI 故障排查截图
_VISION_PROMPT = """\
这是一张深信服 HCI 超融合平台的故障排查截图，请用中文详细描述：

1. 截图界面类型（命令行终端 / Web 管理界面 / 错误弹窗 / 日志输出 / 配置页面等）
2. 截图中所有可见的：IP 地址、主机名、版本号、错误码、状态值、关键数字
3. 截图中的报错信息或异常状态（精确引用原文）
4. 对故障排查有价值的技术细节

输出要求：
- 中文，200字以内
- 聚焦技术信息，不描述视觉设计
- 如截图清晰显示命令输出，请包含关键命令和结果
"""


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
    return desc.strip(), tokens


async def process_images_for_case(
    case_id: str,
    pool: asyncpg.Pool,
    client: AsyncOpenAI,
) -> dict[str, int]:
    """
    处理单个案例的所有待处理图片。

    Returns:
        {"done": N, "failed": N, "skipped": N}
    """
    # 查询该案例未处理的图片
    rows = await pool.fetch(
        """SELECT id, local_path, mime_type
           FROM kbd_image
           WHERE case_id = $1 AND vision_status = 'pending'
           ORDER BY seq""",
        case_id,
    )
    stats = {"done": 0, "failed": 0, "skipped": 0}
    if not rows:
        return stats

    sem = asyncio.Semaphore(settings.VISION_CONCURRENCY)
    images_root = settings.IMAGES_DIR.parent  # kbd cache 目录

    async def _process_one(row: asyncpg.Record) -> None:
        img_id = row["id"]
        local_path = row["local_path"]
        mime = row["mime_type"] or "image/jpeg"

        if not local_path:
            await pool.execute(
                "UPDATE kbd_image SET vision_status='skipped', processed_at=NOW() WHERE id=$1",
                img_id,
            )
            stats["skipped"] += 1
            return

        full_path = images_root / local_path
        if not full_path.exists():
            logger.warning("图片文件不存在: %s", full_path)
            await pool.execute(
                "UPDATE kbd_image SET vision_status='skipped', processed_at=NOW() WHERE id=$1",
                img_id,
            )
            stats["skipped"] += 1
            return

        async with sem:
            try:
                desc, tokens = await _describe_image(client, full_path, mime)
                await pool.execute(
                    """UPDATE kbd_image
                       SET vision_desc=$1, vision_status='done',
                           vision_model=$2, vision_tokens=$3, processed_at=NOW()
                       WHERE id=$4""",
                    desc, settings.VISION_MODEL, tokens, img_id,
                )
                stats["done"] += 1
                logger.debug("图片描述完成 id=%d tokens=%d", img_id, tokens)
            except Exception as exc:
                logger.error("图片描述失败 id=%d 原因=%s", img_id, exc)
                await pool.execute(
                    "UPDATE kbd_image SET vision_status='failed', processed_at=NOW() WHERE id=$1",
                    img_id,
                )
                stats["failed"] += 1

    await asyncio.gather(*[_process_one(row) for row in rows])
    return stats


async def process_images_batch(
    case_ids: list[str],
    pool: asyncpg.Pool,
) -> dict[str, int]:
    """
    批量处理一组案例的图片语义化。

    Returns:
        汇总统计 {"done": N, "failed": N, "skipped": N}
    """
    client = AsyncOpenAI(
        api_key=settings.ZAI_API_KEY,
        base_url=settings.ZAI_BASE_URL,
        timeout=settings.LLM_TIMEOUT,
    )
    total_stats: dict[str, int] = {"done": 0, "failed": 0, "skipped": 0}
    total = len(case_ids)

    for idx, case_id in enumerate(case_ids, 1):
        # 检查是否有待处理图片
        count = await pool.fetchval(
            "SELECT COUNT(*) FROM kbd_image WHERE case_id=$1 AND vision_status='pending'",
            case_id,
        )
        if not count:
            continue

        logger.info("[%d/%d] 处理案例 %s 的 %d 张图片", idx, total, case_id, count)
        stats = await process_images_for_case(case_id, pool, client)
        for k in total_stats:
            total_stats[k] += stats.get(k, 0)

    logger.info(
        "图片语义化完成 done=%d failed=%d skipped=%d",
        total_stats["done"], total_stats["failed"], total_stats["skipped"],
    )
    return total_stats
