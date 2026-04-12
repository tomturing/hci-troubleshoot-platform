"""
scripts/kbd/pipeline.py — KBD 知识生产管道编排（API 调用版）

完整流水线分四个 Stage：

  Stage 1: fetch      抓取 API + 下载图片 → 文件存储（cache/{support_id}/）
  Stage 2: vision     图片语义化（Vision LLM）→ img_N.desc.txt
  Stage 3: import     HTML→MD 转换 + 调用 API 写入 kbd_entry（status=draft）
  Stage 4: classify   AI 分类（调用 kb-service API）→ kbd_entry.ai_category_id

变更（T2-02, T2-03）：
  - Stage 3: 不再直接写数据库，改为调用 `/api/kbd/ingest`
  - Stage 4: 不再本地调用 LLM，改为调用 `/api/kb/classify`

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

import logging
import time
from collections.abc import Sequence
from enum import IntEnum

import asyncpg
import httpx

from .classifier import classify_batch
from .config import settings
from .fetcher import fetch_batch, read_ids_from_excel
from .image_proc import process_images_batch
from .importer import import_batch

logger = logging.getLogger("kbd.pipeline")


class Stage(IntEnum):
    FETCH = 1
    VISION = 2
    IMPORT = 3
    CLASSIFY = 4


async def _create_pool() -> asyncpg.Pool:
    """创建 asyncpg 连接池（用于读取状态，写入通过 API）"""
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

    if not settings.INTERNAL_API_TOKEN:
        raise RuntimeError("INTERNAL_API_TOKEN 未配置，无法调用 kb-service API")

    logger.info(
        "流水线启动 案例数=%d stages=%s",
        len(case_ids),
        [s.name for s in stages],
    )

    pool = await _create_pool()
    http_client = httpx.AsyncClient(timeout=settings.API_TIMEOUT)
    all_stats: dict[str, dict] = {}

    try:
        if Stage.FETCH in stages:
            logger.info("─── Stage 1: 数据抓取 ───")
            t0 = time.monotonic()
            # fetch_batch 现在返回文件存储统计，不依赖数据库写入
            stats = await fetch_batch(case_ids, force=force)
            all_stats["fetch"] = {**stats, "elapsed_s": round(time.monotonic() - t0, 1)}
            logger.info("Stage 1 完成 %s", all_stats["fetch"])

        if Stage.VISION in stages:
            logger.info("─── Stage 2: 图片语义化 ───")
            # 仅处理已抓取完成的案例（有 raw.json）
            from .fetcher import _is_fetched
            done_ids = [cid for cid in case_ids if _is_fetched(cid)]
            t0 = time.monotonic()
            stats = await process_images_batch(done_ids, pool)
            all_stats["vision"] = {**stats, "elapsed_s": round(time.monotonic() - t0, 1)}
            logger.info("Stage 2 完成 %s", all_stats["vision"])

        if Stage.IMPORT in stages:
            logger.info("─── Stage 3: MD 转换 + 入库 ───")
            # 只处理图片已完成或无图片的案例
            # 检查 cache 目录下有 raw.json 且所有图片都有 .desc.txt 或无图片
            ready_ids = await _get_import_ready_ids(case_ids, pool)
            t0 = time.monotonic()
            stats = await import_batch(ready_ids, pool, force_draft=force, client=http_client)
            all_stats["import"] = {**stats, "elapsed_s": round(time.monotonic() - t0, 1)}
            logger.info("Stage 3 完成 %s", all_stats["import"])

        if Stage.CLASSIFY in stages:
            logger.info("─── Stage 4: AI 分类 ───")
            # 只处理已写入 kbd_entry 且 ai_category_id 为空的
            classify_ids = await pool.fetch(
                """SELECT support_id FROM kbd_entry
                   WHERE support_id = ANY($1)
                     AND status = 'draft'
                     AND (ai_category_id IS NULL OR ai_category_id = '')""",
                case_ids,
            )
            classify_case_ids = [r["support_id"] for r in classify_ids]
            t0 = time.monotonic()
            stats = await classify_batch(classify_case_ids, pool)
            all_stats["classify"] = {**stats, "elapsed_s": round(time.monotonic() - t0, 1)}
            logger.info("Stage 4 完成 %s", all_stats["classify"])

    finally:
        await pool.close()
        await http_client.aclose()

    logger.info("流水线全部完成 %s", all_stats)
    return all_stats


async def _get_import_ready_ids(case_ids: list[str], pool: asyncpg.Pool) -> list[str]:
    """
    获取可导入的案例 ID 列表。

    条件：
    1. cache/{support_id}/raw.json 存在（已抓取）
    2. 所有图片都有 .desc.txt 文件（Vision 完成）或无图片
    """
    from .fetcher import _case_dir, _is_fetched

    ready_ids: list[str] = []
    for support_id in case_ids:
        if not _is_fetched(support_id):
            continue

        case_dir = _case_dir(support_id)
        # 检查图片是否全部处理完成
        img_files = list(case_dir.glob("img_*.*"))
        # 过滤掉 .failed 文件
        actual_images = [f for f in img_files if f.suffix not in (".failed", ".txt")]

        if not actual_images:
            # 无图片，可直接导入
            ready_ids.append(support_id)
            continue

        # 检查每张图片是否有对应的 .desc.txt
        all_vision_done = all(
            (case_dir / f"{f.stem}.desc.txt").exists()
            for f in actual_images
        )
        if all_vision_done:
            ready_ids.append(support_id)

    return ready_ids


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
