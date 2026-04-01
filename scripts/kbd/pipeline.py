"""
scripts/kbd/pipeline.py — KBD 知识生产管道编排

完整流水线分四个 Stage：

  Stage 1: fetch      抓取 API + 下载图片 → kbd_raw + kbd_image
  Stage 2: vision     图片语义化（Vision LLM）→ kbd_image.vision_desc
  Stage 3: import     HTML→MD 转换 + 写入 kbd_entry（status=draft）
  Stage 4: classify   AI 分类（198 节点）→ kbd_entry.ai_category_id

每个 Stage 独立可重跑：
  - 已完成的记录自动跳过
  - 失败记录可通过 --stage=N --retry-failed 重试

用法：
  python -m scripts.kbd.run pipeline --excel          # 从 Excel 全量跑
  python -m scripts.kbd.run pipeline --ids 34977,36179
  python -m scripts.kbd.run fetch --ids 34977
  python -m scripts.kbd.run vision --excel
  python -m scripts.kbd.run import --excel
  python -m scripts.kbd.run classify --excel
"""
from __future__ import annotations

import asyncio
import logging
import time
from enum import IntEnum
from typing import Sequence

import asyncpg

from .config import settings
from .fetcher import fetch_batch, read_ids_from_excel
from .image_proc import process_images_batch
from .importer import import_batch
from .classifier import classify_batch

logger = logging.getLogger("kbd.pipeline")


class Stage(IntEnum):
    FETCH = 1
    VISION = 2
    IMPORT = 3
    CLASSIFY = 4


async def _create_pool() -> asyncpg.Pool:
    """创建 asyncpg 连接池"""
    db_url = settings.DATABASE_URL
    # asyncpg 使用 postgresql:// 而非 postgres://
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)
    return await asyncpg.create_pool(
        dsn=db_url,
        min_size=settings.DB_POOL_MIN,
        max_size=settings.DB_POOL_MAX,
    )


async def run_pipeline(
    case_ids: list[str],
    stages: Sequence[Stage] = (Stage.FETCH, Stage.VISION, Stage.IMPORT, Stage.CLASSIFY),
    *,
    force: bool = False,
) -> dict[str, dict]:
    """
    执行指定 stages 的完整流水线。

    Args:
        case_ids: 要处理的案例 ID 列表
        stages: 要执行的阶段（默认全部）
        force: 强制重新处理已完成的记录

    Returns:
        各 stage 的统计结果
    """
    if not case_ids:
        logger.warning("case_ids 为空，流水线退出")
        return {}

    logger.info(
        "流水线启动 案例数=%d stages=%s",
        len(case_ids),
        [s.name for s in stages],
    )

    pool = await _create_pool()
    all_stats: dict[str, dict] = {}

    try:
        if Stage.FETCH in stages:
            logger.info("─── Stage 1: 数据抓取 ───")
            t0 = time.monotonic()
            stats = await fetch_batch(case_ids, pool, force=force)
            all_stats["fetch"] = {**stats, "elapsed_s": round(time.monotonic() - t0, 1)}
            logger.info("Stage 1 完成 %s", all_stats["fetch"])

        if Stage.VISION in stages:
            logger.info("─── Stage 2: 图片语义化 ───")
            # 仅处理本次任务中 fetch_status='done' 的案例
            done_ids = await pool.fetch(
                "SELECT case_id FROM kbd_raw WHERE case_id = ANY($1) AND fetch_status='done'",
                case_ids,
            )
            done_case_ids = [r["case_id"] for r in done_ids]
            t0 = time.monotonic()
            stats = await process_images_batch(done_case_ids, pool)
            all_stats["vision"] = {**stats, "elapsed_s": round(time.monotonic() - t0, 1)}
            logger.info("Stage 2 完成 %s", all_stats["vision"])

        if Stage.IMPORT in stages:
            logger.info("─── Stage 3: MD 转换 + 入库 ───")
            # 只处理图片已完成或无图片的案例
            ready_ids = await pool.fetch(
                """SELECT r.case_id FROM kbd_raw r
                   WHERE r.case_id = ANY($1) AND r.fetch_status = 'done'
                     AND (
                       r.image_count = 0
                       OR NOT EXISTS (
                         SELECT 1 FROM kbd_image i
                         WHERE i.case_id = r.case_id AND i.vision_status = 'pending'
                       )
                     )""",
                case_ids,
            )
            ready_case_ids = [r["case_id"] for r in ready_ids]
            t0 = time.monotonic()
            stats = await import_batch(ready_case_ids, pool, force_draft=force)
            all_stats["import"] = {**stats, "elapsed_s": round(time.monotonic() - t0, 1)}
            logger.info("Stage 3 完成 %s", all_stats["import"])

        if Stage.CLASSIFY in stages:
            logger.info("─── Stage 4: AI 分类 ───")
            # 只处理已写入 kbd_entry 且 ai_category_id 为空的
            classify_ids = await pool.fetch(
                """SELECT case_id FROM kbd_entry
                   WHERE case_id = ANY($1)
                     AND status = 'draft'
                     AND (ai_category_id IS NULL OR ai_category_id = '')""",
                case_ids,
            )
            classify_case_ids = [r["case_id"] for r in classify_ids]
            t0 = time.monotonic()
            stats = await classify_batch(classify_case_ids, pool)
            all_stats["classify"] = {**stats, "elapsed_s": round(time.monotonic() - t0, 1)}
            logger.info("Stage 4 完成 %s", all_stats["classify"])

    finally:
        await pool.close()

    logger.info("流水线全部完成 %s", all_stats)
    return all_stats


async def run_from_excel(
    stages: Sequence[Stage] = (Stage.FETCH, Stage.VISION, Stage.IMPORT, Stage.CLASSIFY),
    *,
    force: bool = False,
    limit: int | None = None,
) -> dict[str, dict]:
    """从 Excel 文件读取全量 ID 并运行流水线"""
    case_ids = read_ids_from_excel()
    if limit:
        case_ids = case_ids[:limit]
    logger.info("从 Excel 读取 %d 个案例 ID（limit=%s）", len(case_ids), limit)
    return await run_pipeline(case_ids, stages=stages, force=force)
