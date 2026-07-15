"""
data-pipeline/kbd/pipeline.py — KBD 知识生产管道编排（API 调用版）

完整流水线分四个 Stage：

  Stage 1: fetch      抓取 API + 下载图片 → 文件存储（cache/{support_id}/）
  Stage 2: vision     图片语义化（Vision LLM）→ 写 kbd_entry.images_json.desc
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
  python -m kbd.run pipeline --excel          # 从 Excel 全量跑
  python -m kbd.run pipeline --ids 34977,36179
  python -m kbd.run pipeline --excel --resume  # 从上次中断处继续
  python -m kbd.run pipeline --excel --failed-only  # 仅处理失败案例
  python -m kbd.run fetch --ids 34977
  python -m kbd.run vision --excel
  python -m kbd.run import --excel
  python -m kbd.run classify --excel
"""
from __future__ import annotations

import logging
import time
from collections.abc import Iterable, Sequence
from enum import IntEnum

import asyncpg
import httpx

from .classifier import classify_batch
from .config import settings
from .extract_signals import extract_signals_batch
from .fetcher import _is_fetched, fetch_batch, get_failed_fetch_ids, read_ids_from_excel
from .image_proc import process_images_batch
from .importer import import_batch
from .progress import (
    finish_progress,
    generate_run_id,
    get_completed_ids_for_stage,
    init_progress,
    save_progress,
    update_stage_status,
)

logger = logging.getLogger("kbd.pipeline")


class Stage(IntEnum):
    """流水线阶段枚举。

    新架构 DAG（有向无环，依赖单向）：
      FETCH → IMPORT → VISION → CLASSIFY
    VISION 移到 IMPORT 之后（因为 VISION 需要 kbd_entry.id 和 kbd_image），
    IMPORT 原子写入 kbd_entry + kbd_image，消除循环依赖。
    """
    FETCH = 1
    IMPORT = 2  # 原子写入 kbd_entry + kbd_image
    VISION = 3  # 读 kbd_image，调 Vision LLM，更新 images_json + rebuild content_md
    CLASSIFY = 4
    EXTRACT_SIGNALS = 5  # 关键信号分级抽取：LLM 抽取 signals_json 并写回 kbd_entry


# ─── DAG 依赖声明（拓扑排序 P0-② 增强）─────────────────────────────────────────

# Stage DAG：每个 stage 声明其所有前置依赖（直接前置，闭包由 resolve_stages 自动展开）
STAGE_DEPENDENCIES: dict[Stage, tuple[Stage, ...]] = {
    Stage.FETCH: (),                                                  # 无前置
    Stage.IMPORT: (Stage.FETCH,),                                     # 需要 raw.json + 本地图片
    Stage.VISION: (Stage.IMPORT,),                                    # 需要 kbd_entry + kbd_image
    Stage.CLASSIFY: (Stage.VISION,),                                  # 需要 content_md 含视觉描述（完整上下文分类更准）
    Stage.EXTRACT_SIGNALS: (Stage.CLASSIFY,),                          # 需要 ai_category_id 作为领域上下文
}


def resolve_stages(requested: Iterable[Stage]) -> list[Stage]:
    """DAG 闭包 + 拓扑序展开。

    自动补齐用户请求 stage 的所有前置依赖（如只跑 VISION，自动拉取 IMPORT + FETCH）。
    解决"只跑 VISION 但缺少前置导致 pipeline 静默失败"的边界问题。

    Args:
        requested: 用户传入的 stages（含去重）

    Returns:
        按拓扑序（FETCH → IMPORT → VISION → CLASSIFY）排列的全部 stage 列表，
        包含用户请求 + 全部传递依赖
    """
    requested_set = set(requested)
    closure = set(requested_set)
    queue: list[Stage] = list(requested_set)
    while queue:
        s = queue.pop()
        for dep in STAGE_DEPENDENCIES.get(s, ()):
            if dep not in closure:
                closure.add(dep)
                queue.append(dep)
    # 按 Stage 枚举值排序（FETCH=1 → CLASSIFY=4，天然拓扑序）
    return sorted(closure, key=lambda x: int(x))


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
    kbd_ids: list[str],
    stages: Sequence[Stage] = (Stage.FETCH, Stage.VISION, Stage.IMPORT, Stage.CLASSIFY, Stage.EXTRACT_SIGNALS),
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
    执行指定 stages 的完整流水线（按 DAG 拓扑序自动补齐前置依赖）。

    Args:
        kbd_ids: 要处理的案例 ID 列表
        stages: 要执行的阶段（默认全部）。DAG 拓扑补齐：传入 [VISION] 会自动展开为
            [FETCH, IMPORT, VISION]（含全部传递依赖）。详见 `resolve_stages`。
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
    if not kbd_ids:
        logger.warning("kbd_ids 为空，流水线退出")
        return {}, run_id or generate_run_id()

    if not settings.INTERNAL_API_TOKEN:
        raise RuntimeError("INTERNAL_API_TOKEN 未配置，无法调用 kb-service API")

    # ── DAG 拓扑展开（P0-② 增强）────────────────────────────────────────────
    # 用户传 [VISION] 自动补齐为 [FETCH, IMPORT, VISION]，避免"VSION 静默失败"。
    resolved = resolve_stages(stages)
    if set(resolved) != set(stages):
        added = [s.name for s in resolved if s not in set(stages)]
        logger.info(
            "DAG 拓扑补齐: 用户请求 %s，自动追加前置依赖 %s -> 实际执行 %s",
            [s.name for s in stages], added, [s.name for s in resolved],
        )
    stages = resolved

    # ── 进度追踪（可观测性日志，不参与正确性，P1-⑥）────────────────────────────
    progress = None

    # resume 模式：不再依赖 progress.json 文件。
    # 每个 stage 通过 DB 状态自动跳过已完成案例（_get_import_ready_ids /
    # _get_vision_ready_ids / _db_vision_status 等都是 DB 查询）。
    # 这样 --stages X 单独重跑任意案例无需清理 progress 文件，并能从
    # 任何异常中断（崩溃 / 强制终止）中恢复，DB 状态才是真相之源。
    if resume:
        logger.info(
            "Resume 模式：依赖 DB 状态自动跳过已完成案例（不再依赖 progress.json）"
        )

    # 生成 run_id（若未传）。progress 仅作为可观测性日志（各 stage 写一次）。
    if run_id is None:
        run_id = generate_run_id()
    stage_names = [s.name.lower() for s in stages]
    progress = init_progress(run_id, kbd_ids, stage_names)

    # ── 提前创建连接池（failed_only 和各 stage 都需要使用）──
    pool = await _create_pool()

    # ── 失败案例筛选 ──
    if failed_only:
        logger.info("Failed-only 模式：筛选失败案例")
        failed_fetch = get_failed_fetch_ids(kbd_ids)
        failed_vision = await _db_failed_vision_ids(kbd_ids, pool)
        failed_ids = list(set(failed_fetch + failed_vision))
        if not failed_ids:
            logger.info("没有失败的案例需要处理")
            finish_progress(progress)
            return {"failed_only": {"skipped": len(kbd_ids)}}, run_id
        kbd_ids = failed_ids
        logger.info("筛选出 %d 个失败案例（fetch=%d, vision=%d）",
                    len(kbd_ids), len(failed_fetch), len(failed_vision))
        # 重新初始化进度（针对筛选后的案例）
        stage_names = [s.name.lower() for s in stages]
        progress = init_progress(run_id, kbd_ids, stage_names)

    logger.info(
        "流水线启动 案例数=%d stages=%s run_id=%s resume=%s",
        len(kbd_ids),
        [s.name for s in stages],
        run_id,
        resume,
    )

    http_client = httpx.AsyncClient(timeout=settings.API_TIMEOUT)
    all_stats: dict[str, dict] = {}

    try:
        if Stage.FETCH in stages:
            logger.info("─── Stage 1: 数据抓取 ───")
            fetch_ids = kbd_ids
            t0 = time.monotonic()
            stats = await fetch_batch(fetch_ids, force=force_fetch)
            all_stats["fetch"] = {**stats, "elapsed_s": round(time.monotonic() - t0, 1)}
            logger.info("Stage 1 完成 %s", all_stats["fetch"])

            # 更新进度
            for cid in fetch_ids:
                status = "done" if _is_fetched(cid) else "failed"
                update_stage_status(progress, "fetch", cid, status)
            save_progress(run_id, progress)

        if Stage.IMPORT in stages:
            logger.info("─── Stage 2: 语义提取 + 原子入库 ───")
            ready_ids = await _get_import_ready_ids(kbd_ids, pool)
            import_ids = ready_ids

            t0 = time.monotonic()
            stats = await import_batch(
                import_ids, pool, override=override, override_status=override_status, client=http_client
            )
            all_stats["import"] = {**stats, "elapsed_s": round(time.monotonic() - t0, 1)}
            logger.info("Stage 2 完成 %s", all_stats["import"])

            # 更新进度
            for cid in import_ids:
                row = await pool.fetchrow(
                    """SELECT support_id FROM kbd_entry WHERE support_id = $1""",
                    cid,
                )
                status = "done" if row else "failed"
                update_stage_status(progress, "import", cid, status)
            save_progress(run_id, progress)

        if Stage.VISION in stages:
            logger.info("─── Stage 3: 图片语义化 ───")
            # 新架构：VISION 在 IMPORT 之后，仅处理 kbd_entry + kbd_image 已就位的案例
            vision_ids = await _get_vision_ready_ids(kbd_ids, pool)

            t0 = time.monotonic()
            stats = await process_images_batch(vision_ids, pool)
            all_stats["vision"] = {**stats, "elapsed_s": round(time.monotonic() - t0, 1)}
            logger.info("Stage 3 完成 %s", all_stats["vision"])

            # 更新进度（基于 DB images_json desc 完整性判据，详见 _db_vision_status）
            for cid in vision_ids:
                status = await _db_vision_status(pool, cid)
                update_stage_status(progress, "vision", cid, status)
            save_progress(run_id, progress)

        if Stage.CLASSIFY in stages:
            logger.info("─── Stage 4: AI 分类 ───")
            classify_rows = await pool.fetch(
                """SELECT support_id FROM kbd_entry
                   WHERE support_id = ANY($1)
                     AND status = 'draft'
                     AND (ai_category_id IS NULL OR ai_category_id = '')""",
                kbd_ids,
            )
            classify_ids_all = [r["support_id"] for r in classify_rows]
            classify_kbd_ids = classify_ids_all

            t0 = time.monotonic()
            stats = await classify_batch(classify_kbd_ids, pool)
            all_stats["classify"] = {**stats, "elapsed_s": round(time.monotonic() - t0, 1)}
            logger.info("Stage 4 完成 %s", all_stats["classify"])

            # 更新进度
            for cid in classify_kbd_ids:
                row = await pool.fetchrow(
                    """SELECT ai_category_id FROM kbd_entry WHERE support_id = $1""",
                    cid,
                )
                status = "done" if row and row["ai_category_id"] else "failed"
                update_stage_status(progress, "classify", cid, status)
            save_progress(run_id, progress)

        if Stage.EXTRACT_SIGNALS in stages:
            logger.info("─── Stage 5: 关键信号分级抽取 ───")
            # 仅处理已分类且 signals_json 为空的 draft 案例
            extract_rows = await pool.fetch(
                """SELECT support_id FROM kbd_entry
                   WHERE support_id = ANY($1)
                     AND status = 'draft'
                     AND (signals_json IS NULL OR signals_json = '[]'::jsonb)""",
                kbd_ids,
            )
            extract_ids_all = [r["support_id"] for r in extract_rows]

            # Resume 模式：跳过已完成的案例
            extract_kbd_ids = extract_ids_all
            if resume and progress:
                completed_ids = get_completed_ids_for_stage(progress, "extract")
                extract_kbd_ids = [cid for cid in extract_ids_all if cid not in completed_ids]
                skipped = len(extract_ids_all) - len(extract_kbd_ids)
                if skipped > 0:
                    logger.info("Resume 跳过 %d 个已完成的 extract 案例", skipped)

            t0 = time.monotonic()
            stats = await extract_signals_batch(extract_kbd_ids, pool)
            all_stats["extract"] = {**stats, "elapsed_s": round(time.monotonic() - t0, 1)}
            logger.info("Stage 5 完成 %s", all_stats["extract"])

            # 更新进度
            for cid in extract_kbd_ids:
                row = await pool.fetchrow(
                    """SELECT signals_json FROM kbd_entry WHERE support_id = $1""",
                    cid,
                )
                done = row and row["signals_json"] and row["signals_json"] != "[]"
                update_stage_status(progress, "extract", cid, "done" if done else "failed")
            save_progress(run_id, progress)

        # 标记进度完成
        finish_progress(progress)

    finally:
        await pool.close()
        await http_client.aclose()

    logger.info("流水线全部完成 run_id=%s %s", run_id, all_stats)
    return all_stats, run_id


async def _get_import_ready_ids(kbd_ids: list[str], pool: asyncpg.Pool) -> list[str]:
    """
    获取可导入的案例 ID 列表。

    新架构：仅检查 FETCH 完成即可入库；图片随 IMPORT 原子写入 kbd_image，
    Vision 在 IMPORT 之后跑（读 kbd_image，写 images_json.desc）。解除了旧架构下
    "IMPORT 需要 .desc.txt（VISION 旧产物）" 的循环依赖（.desc.txt 机制已彻底移除）。
    """
    ready_ids: list[str] = []
    for support_id in kbd_ids:
        if not _is_fetched(support_id):
            continue
        ready_ids.append(support_id)
    return ready_ids


async def _get_vision_ready_ids(kbd_ids: list[str], pool: asyncpg.Pool) -> list[str]:
    """获取可执行 Vision 的案例 ID 列表。

    新架构：VISION 在 IMPORT 之后，仅处理 kbd_entry + kbd_image 已就位的案例。
    过滤条件：cache/{id}/raw.json 存在（FETCH 完成）且 kbd_entry 与 kbd_image 已写入。
    """
    if not kbd_ids:
        return []
    rows = await pool.fetch(
        """
        SELECT e.support_id
        FROM kbd_entry e
        WHERE e.support_id = ANY($1)
          AND EXISTS (SELECT 1 FROM kbd_image i WHERE i.kbd_entry_id = e.id)
        """,
        kbd_ids,
    )
    return [r["support_id"] for r in rows]



async def _db_failed_vision_ids(
    kbd_ids: list[str],
    pool: asyncpg.Pool | None = None,
) -> list[str]:
    """从 DB 筛选 VISION 失败的案例（P1-6 跟进：与 _db_vision_status 同源判据，避免回滚到 image_proc）。

    判据：kbd_entry 存在且 images_json 中存在 desc 为空的 seq（与 _db_vision_status 的 'failed'
    判据一致），且 kbd_image 表非空（有图但未识别完成）。

    Args:
        kbd_ids: support_id 列表
        pool: asyncpg 连接池（不传则按 settings.DATABASE_URL 自建）
    """
    if not kbd_ids:
        return []
    close_pool = False
    if pool is None:
        pool = await asyncpg.create_pool(
            dsn=settings.DATABASE_URL.replace("postgres://", "postgresql://", 1)
        )
        close_pool = True
    try:
        rows = await pool.fetch(
            """
            SELECT e.support_id
            FROM kbd_entry e
            WHERE e.support_id = ANY($1)
              AND EXISTS (SELECT 1 FROM kbd_image i WHERE i.kbd_entry_id = e.id)
              AND EXISTS (
                  SELECT 1 FROM jsonb_array_elements(e.images_json) AS img
                  WHERE (img->>'desc') IS NULL OR length(btrim(img->>'desc')) = 0
              )
            """,
            kbd_ids,
        )
        return [r["support_id"] for r in rows]
    finally:
        if close_pool:
            await pool.close()


async def _db_vision_status(pool: asyncpg.Pool, support_id: str) -> str:
    """基于 DB images_json desc 完整性判读 VISION 完成状态（P0-4：进度追踪改判数据库）。

    判据：
      - kbd_entry 不存在 → 'failed'（异常，前置 IMPORT 失败）
      - images_json 为空 → 'failed'（无图片案例不应进入 VISION 阶段）
      - 存在 seq 但 desc 为空 → 'failed'（VISION 未完成）
      - 所有 seq 的 desc 都非空 → 'done'
    """
    row = await pool.fetchrow(
        """
        SELECT
            e.id AS kbd_id,
            e.images_json,
            (SELECT COUNT(*) FROM kbd_image i WHERE i.kbd_entry_id = e.id) AS img_count
        FROM kbd_entry e
        WHERE e.support_id = $1
        """,
        support_id,
    )
    if row is None:
        return "failed"
    images_json = row["images_json"] or []
    img_count = row["img_count"]
    # 无图片案例默认 done（与旧行为一致）
    if img_count == 0:
        return "done"
    if not images_json:
        return "failed"
    # 兼容旧格式：images_json 可能含非 dict 元素（list[str] 等遗留数据）。
    # 非 dict 元素计为"未完成"，避免 AttributeError 并触发重新识图/入库。
    dict_items = [item for item in images_json if isinstance(item, dict)]
    stale_items = [item for item in images_json if not isinstance(item, dict)]
    incomplete = [item for item in dict_items if not (item.get("desc") or "").strip()]
    return "failed" if (incomplete or stale_items) else "done"


async def run_from_excel(
    stages: Sequence[Stage] = (Stage.FETCH, Stage.VISION, Stage.IMPORT, Stage.CLASSIFY, Stage.EXTRACT_SIGNALS),
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
    kbd_ids = read_ids_from_excel()
    if limit:
        kbd_ids = kbd_ids[:limit]
    logger.info("从 Excel 读取 %d 个案例 ID（limit=%s）", len(kbd_ids), limit)
    return await run_pipeline(
        kbd_ids,
        stages=stages,
        force_fetch=force_fetch,
        override=override,
        override_status=override_status,
        resume=resume,
        resume_run_id=resume_run_id,
        failed_only=failed_only,
        run_id=run_id,
    )
