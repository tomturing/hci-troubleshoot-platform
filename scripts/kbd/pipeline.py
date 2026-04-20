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

变更（进度追踪 v1）：
  - 支持 run_id 参数（YYYYMMDD_HHMMSS 格式）
  - 支持 resume 模式（从上次中断处继续）
  - 支持 failed_only 模式（仅处理失败案例）
  - 每个 Stage 完成后更新 progress.json

每个 Stage 独立可重跑：
  - 已完成的记录自动跳过
  - 失败记录可通过 --stage=N --retry-failed 重试

用法：
  python -m scripts.kbd.run pipeline --excel          # 从 Excel 全量跑
  python -m scripts.kbd.run pipeline --ids 34977,36179
  python -m scripts.kbd.run pipeline --excel --resume  # 从上次中断处继续
  python -m scripts.kbd.run pipeline --excel --failed-only  # 仅处理失败案例
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
from .fetcher import fetch_batch, read_ids_from_excel, get_failed_fetch_ids, _is_fetched, _case_dir
from .image_proc import process_images_batch, get_failed_vision_ids, _has_failed_vision, _find_images
from .importer import import_batch
from .progress import (
    generate_run_id,
    init_progress,
    save_progress,
    load_progress,
    update_stage_status,
    finish_progress,
    find_latest_progress_file,
    get_completed_ids_for_stage,
    ALL_STAGES,
)

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
    force_fetch: bool = False,
    override: bool = False,
    override_status: list[str] | None = None,
    resume: bool = False,
    resume_run_id: str | None = None,
    failed_only: bool = False,
    run_id: str | None = None,
) -> tuple[dict[str, dict], str]:
    """
    执行指定 stages 的完整流水线。

    Args:
        case_ids: 要处理的案例 ID 列表
        stages: 要执行的阶段（默认全部）
        force_fetch: 强制重新抓取已完成的案例（仅影响 Stage 1）
        override: 强制覆盖已存在的记录（仅影响 Stage 3 导入阶段）
        override_status: 仅覆盖指定状态的记录。None=默认仅draft；['all']=所有状态
        resume: 从上次中断处继续（加载 progress.json，跳过已完成案例）
        resume_run_id: 指定要恢复的 run_id（不传则自动查找最新的 progress 文件）
        failed_only: 仅处理失败的案例（有 .failed 标记或识别为无文字）
        run_id: 本次运行的 run_id（不传则自动生成）

    Returns:
        (各 stage 的统计结果, 实际使用的 run_id)
    """
    if not case_ids:
        logger.warning("case_ids 为空，流水线退出")
        return {}, run_id or generate_run_id()

    if not settings.INTERNAL_API_TOKEN:
        raise RuntimeError("INTERNAL_API_TOKEN 未配置，无法调用 kb-service API")

    # ── 进度追踪初始化 ──
    if run_id is None:
        if resume and resume_run_id is None:
            # resume 模式：自动查找最新的 progress 文件
            resume_run_id = find_latest_progress_file()
            if resume_run_id:
                logger.info("Resume 模式：找到最新进度文件 run_id=%s", resume_run_id)
            else:
                logger.warning("Resume 模式：未找到进度文件，将从头开始")
        run_id = generate_run_id()

    progress = None
    if resume and resume_run_id:
        progress = load_progress(resume_run_id)
        if progress:
            logger.info("已加载进度文件 run_id=%s cases=%d", resume_run_id, len(progress.get("cases", {})))
        else:
            logger.warning("进度文件加载失败，将从头开始 run_id=%s", resume_run_id)

    # 初始化进度（如果没有加载到）
    if progress is None:
        stage_names = [s.name.lower() for s in stages]
        progress = init_progress(run_id, case_ids, stage_names)

    # ── 失败案例筛选 ──
    if failed_only:
        logger.info("Failed-only 模式：筛选失败案例")
        failed_fetch = get_failed_fetch_ids(case_ids)
        failed_vision = get_failed_vision_ids(case_ids)
        failed_ids = list(set(failed_fetch + failed_vision))
        if not failed_ids:
            logger.info("没有失败的案例需要处理")
            finish_progress(progress)
            return {"failed_only": {"skipped": len(case_ids)}}, run_id
        case_ids = failed_ids
        logger.info("筛选出 %d 个失败案例（fetch=%d, vision=%d）",
                    len(case_ids), len(failed_fetch), len(failed_vision))
        # 重新初始化进度（针对筛选后的案例）
        stage_names = [s.name.lower() for s in stages]
        progress = init_progress(run_id, case_ids, stage_names)

    logger.info(
        "流水线启动 案例数=%d stages=%s run_id=%s resume=%s",
        len(case_ids),
        [s.name for s in stages],
        run_id,
        resume,
    )

    pool = await _create_pool()
    http_client = httpx.AsyncClient(timeout=settings.API_TIMEOUT)
    all_stats: dict[str, dict] = {}

    try:
        if Stage.FETCH in stages:
            logger.info("─── Stage 1: 数据抓取 ───")
            # Resume 模式：跳过已完成的案例
            fetch_ids = case_ids
            if resume and progress:
                completed_ids = get_completed_ids_for_stage(progress, "fetch")
                fetch_ids = [cid for cid in case_ids if cid not in completed_ids]
                skipped = len(case_ids) - len(fetch_ids)
                if skipped > 0:
                    logger.info("Resume 跳过 %d 个已完成的 fetch 案例", skipped)

            t0 = time.monotonic()
            stats = await fetch_batch(fetch_ids, force=force_fetch)
            all_stats["fetch"] = {**stats, "elapsed_s": round(time.monotonic() - t0, 1)}
            logger.info("Stage 1 完成 %s", all_stats["fetch"])

            # 更新进度
            for cid in fetch_ids:
                status = "done" if _is_fetched(cid) else "failed"
                update_stage_status(progress, "fetch", cid, status)
            save_progress(run_id, progress)

        if Stage.VISION in stages:
            logger.info("─── Stage 2: 图片语义化 ───")
            # 仅处理已抓取完成的案例
            done_ids = [cid for cid in case_ids if _is_fetched(cid)]

            # Resume 模式：跳过已完成的案例
            vision_ids = done_ids
            if resume and progress:
                completed_ids = get_completed_ids_for_stage(progress, "vision")
                vision_ids = [cid for cid in done_ids if cid not in completed_ids]
                skipped = len(done_ids) - len(vision_ids)
                if skipped > 0:
                    logger.info("Resume 跳过 %d 个已完成的 vision 案例", skipped)

            t0 = time.monotonic()
            stats = await process_images_batch(vision_ids, pool)
            all_stats["vision"] = {**stats, "elapsed_s": round(time.monotonic() - t0, 1)}
            logger.info("Stage 2 完成 %s", all_stats["vision"])

            # 更新进度
            for cid in vision_ids:
                case_dir = _case_dir(cid)
                images = _find_images(case_dir)
                all_done = all(
                    (case_dir / f"{img.stem}.desc.txt").exists()
                    for img in images
                ) if images else True
                status = "done" if all_done and not _has_failed_vision(case_dir) else "failed"
                update_stage_status(progress, "vision", cid, status)
            save_progress(run_id, progress)

        if Stage.IMPORT in stages:
            logger.info("─── Stage 3: MD 转换 + 入库 ───")
            ready_ids = await _get_import_ready_ids(case_ids, pool)

            # Resume 模式：跳过已完成的案例
            import_ids = ready_ids
            if resume and progress:
                completed_ids = get_completed_ids_for_stage(progress, "import")
                import_ids = [cid for cid in ready_ids if cid not in completed_ids]
                skipped = len(ready_ids) - len(import_ids)
                if skipped > 0:
                    logger.info("Resume 跳过 %d 个已完成的 import 案例", skipped)

            t0 = time.monotonic()
            stats = await import_batch(
                import_ids, pool, override=override, override_status=override_status, client=http_client
            )
            all_stats["import"] = {**stats, "elapsed_s": round(time.monotonic() - t0, 1)}
            logger.info("Stage 3 完成 %s", all_stats["import"])

            # 更新进度
            for cid in import_ids:
                row = await pool.fetchrow(
                    """SELECT support_id FROM kbd_entry WHERE support_id = $1""",
                    cid,
                )
                status = "done" if row else "failed"
                update_stage_status(progress, "import", cid, status)
            save_progress(run_id, progress)

        if Stage.CLASSIFY in stages:
            logger.info("─── Stage 4: AI 分类 ───")
            classify_rows = await pool.fetch(
                """SELECT support_id FROM kbd_entry
                   WHERE support_id = ANY($1)
                     AND status = 'draft'
                     AND (ai_category_id IS NULL OR ai_category_id = '')""",
                case_ids,
            )
            classify_ids_all = [r["support_id"] for r in classify_rows]

            # Resume 模式：跳过已完成的案例
            classify_case_ids = classify_ids_all
            if resume and progress:
                completed_ids = get_completed_ids_for_stage(progress, "classify")
                classify_case_ids = [cid for cid in classify_ids_all if cid not in completed_ids]
                skipped = len(classify_ids_all) - len(classify_case_ids)
                if skipped > 0:
                    logger.info("Resume 跳过 %d 个已完成的 classify 案例", skipped)

            t0 = time.monotonic()
            stats = await classify_batch(classify_case_ids, pool)
            all_stats["classify"] = {**stats, "elapsed_s": round(time.monotonic() - t0, 1)}
            logger.info("Stage 4 完成 %s", all_stats["classify"])

            # 更新进度
            for cid in classify_case_ids:
                row = await pool.fetchrow(
                    """SELECT ai_category_id FROM kbd_entry WHERE support_id = $1""",
                    cid,
                )
                status = "done" if row and row["ai_category_id"] else "failed"
                update_stage_status(progress, "classify", cid, status)
            save_progress(run_id, progress)

        # 标记进度完成
        finish_progress(progress)

    finally:
        await pool.close()
        await http_client.aclose()

    logger.info("流水线全部完成 run_id=%s %s", run_id, all_stats)
    return all_stats, run_id


async def _get_import_ready_ids(case_ids: list[str], pool: asyncpg.Pool) -> list[str]:
    """
    获取可导入的案例 ID 列表。

    条件：
    1. cache/{support_id}/raw.json 存在（已抓取）
    2. 所有图片都有 .desc.txt 文件（Vision 完成）或无图片
    """
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
    force_fetch: bool = False,
    override: bool = False,
    override_status: list[str] | None = None,
    limit: int | None = None,
    resume: bool = False,
    resume_run_id: str | None = None,
    failed_only: bool = False,
    run_id: str | None = None,
) -> tuple[dict[str, dict], str]:
    """从 Excel 文件读取全量 ID 并运行流水线"""
    case_ids = read_ids_from_excel()
    if limit:
        case_ids = case_ids[:limit]
    logger.info("从 Excel 读取 %d 个案例 ID（limit=%s）", len(case_ids), limit)
    return await run_pipeline(
        case_ids,
        stages=stages,
        force_fetch=force_fetch,
        override=override,
        override_status=override_status,
        resume=resume,
        resume_run_id=resume_run_id,
        failed_only=failed_only,
        run_id=run_id,
    )