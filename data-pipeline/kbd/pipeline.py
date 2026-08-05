"""
data-pipeline/kbd/pipeline.py — KBD 知识生产管道编排（API 调用版）

完整流水线分六个 Stage：

  Stage 1: fetch      抓取 API + 下载图片 → 文件存储（cache/{support_id}/）
  Stage 2: import     语义转换 + 原子写入 kbd_entry/kbd_image（status=draft）
  Stage 3: vision     图片语义化（Vision LLM）→ 写 kbd_entry.images_json
  Stage 4: classify   AI 分类（调用 kb-service API）→ kbd_entry.ai_category_id
  Stage 5: extract    关键信号抽取（调用 kb-service API）→ kbd_entry.signals_json
  Stage 6: audit      只读审计 qfk_log Proposal 与运行时契约

变更（T2-02, T2-03）：
  - Stage 2: 不再直接写数据库，改为调用 `/api/kbd/ingest`
  - Stage 4: 不再本地调用 LLM，改为调用 `/api/kb/classify`

变更（进度追踪 v1）：
  - 支持 run_id 参数（YYYYMMDD_HHMMSS 格式）
  - 支持 resume 模式（从上次中断处继续）
  - 支持 failed_only 模式（仅处理失败案例）
  - 每个 Stage 完成后更新 progress.json

每个 Stage 可重跑：
  - 已完成的记录自动跳过
  - Fetch/Vision 失败记录可通过 --failed-only 重试

用法：
  python -m kbd.run pipeline --excel          # 从 Excel 全量跑
  python -m kbd.run pipeline --ids 34977,36179
  python -m kbd.run pipeline --excel --resume  # 从上次中断处继续
  python -m kbd.run pipeline --excel --failed-only  # 仅处理失败案例
  python -m kbd.run fetch --ids 34977
  python -m kbd.run vision --excel
  python -m kbd.run import --excel
  python -m kbd.run classify --excel
  python -m kbd.run extract-signals --excel
  python -m kbd.run audit-log-signals --all
"""
from __future__ import annotations

import json
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
      FETCH → IMPORT → VISION → CLASSIFY → EXTRACT_SIGNALS → AUDIT_LOG_SIGNALS
    VISION 移到 IMPORT 之后（因为 VISION 需要 kbd_entry.id 和 kbd_image），
    IMPORT 原子写入 kbd_entry + kbd_image，消除循环依赖。
    """
    FETCH = 1
    IMPORT = 2  # 原子写入 kbd_entry + kbd_image
    VISION = 3  # 读 kbd_image，调 Vision LLM，更新 images_json + rebuild content_md
    CLASSIFY = 4
    EXTRACT_SIGNALS = 5  # 关键信号分级抽取：LLM 抽取 signals_json 并写回 kbd_entry
    AUDIT_LOG_SIGNALS = 6  # 只读审计：qfk_log Proposal 是否符合共享运行时契约


# ─── DAG 依赖声明（拓扑排序 P0-② 增强）─────────────────────────────────────────

# Stage DAG：每个 stage 声明其所有前置依赖（直接前置，闭包由 resolve_stages 自动展开）
STAGE_DEPENDENCIES: dict[Stage, tuple[Stage, ...]] = {
    Stage.FETCH: (),                                                  # 无前置
    Stage.IMPORT: (Stage.FETCH,),                                     # 需要 raw.json + 本地图片
    Stage.VISION: (Stage.IMPORT,),                                    # 需要 kbd_entry + kbd_image
    Stage.CLASSIFY: (Stage.VISION,),                                  # 需要 content_md 含视觉描述（完整上下文分类更准）
    Stage.EXTRACT_SIGNALS: (Stage.CLASSIFY,),                          # 需要 ai_category_id 作为领域上下文
    Stage.AUDIT_LOG_SIGNALS: (Stage.EXTRACT_SIGNALS,),                 # 审计生产后的 signals_json
}


def resolve_stages(requested: Iterable[Stage]) -> list[Stage]:
    """DAG 闭包 + 拓扑序展开。

    自动补齐用户请求 stage 的所有前置依赖（如只跑 VISION，自动拉取 IMPORT + FETCH）。
    解决"只跑 VISION 但缺少前置导致 pipeline 静默失败"的边界问题。

    Args:
        requested: 用户传入的 stages（含去重）

    Returns:
        按拓扑序（FETCH → IMPORT → VISION → CLASSIFY → EXTRACT → AUDIT）排列的全部 stage 列表，
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
    # 按 Stage 枚举值排序（FETCH=1 → AUDIT_LOG_SIGNALS=6，天然拓扑序）
    return sorted(closure, key=lambda x: int(x))


async def _create_pool() -> asyncpg.Pool:
    """创建 asyncpg 连接池（用于读取状态，写入通过 API）"""
    return await asyncpg.create_pool(
        dsn=settings.asyncpg_database_url,
        min_size=settings.DB_POOL_MIN,
        max_size=settings.DB_POOL_MAX,
    )


async def run_pipeline(
    kbd_ids: list[str],
    stages: Sequence[Stage] = (
        Stage.FETCH,
        Stage.IMPORT,
        Stage.VISION,
        Stage.CLASSIFY,
        Stage.EXTRACT_SIGNALS,
        Stage.AUDIT_LOG_SIGNALS,
    ),
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
        override: 强制覆盖已存在的记录（仅影响 Stage 2 导入阶段）
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
            await pool.close()
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
    # 只有本次运行中前置阶段成功的案例才能沿 DAG 向下游传播。
    # 不能再用数据库中的历史行替代本次 IMPORT 的执行结果。
    active_ids = list(kbd_ids)

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
            active_ids = [cid for cid in fetch_ids if _is_fetched(cid)]
            save_progress(run_id, progress)

        if Stage.IMPORT in stages:
            logger.info("─── Stage 2: 语义提取 + 原子入库 ───")
            _mark_dependency_blocked(progress, "import", kbd_ids, active_ids)
            ready_ids = await _get_import_ready_ids(active_ids, pool)
            import_ids = ready_ids

            t0 = time.monotonic()
            stats = await import_batch(
                import_ids, pool, override=override, override_status=override_status, client=http_client
            )
            all_stats["import"] = {**stats, "elapsed_s": round(time.monotonic() - t0, 1)}
            logger.info("Stage 2 完成 %s", all_stats["import"])

            # 本次 API 返回是 Import 阶段的唯一真相源。即使 DB 中有历史旧行，
            # 本次 override 失败也必须记为 failed，且不能进入下游。
            import_results = stats.get("results", {})
            successful_import_statuses = {"created", "overridden", "skipped"}
            for cid in import_ids:
                status = (
                    "done"
                    if import_results.get(cid) in successful_import_statuses
                    else "failed"
                )
                update_stage_status(progress, "import", cid, status)
            active_ids = [
                cid for cid in import_ids
                if import_results.get(cid) in successful_import_statuses
            ]
            all_stats["import"]["blocked_by_dependency"] = len(kbd_ids) - len(import_ids)
            save_progress(run_id, progress)

        if Stage.VISION in stages:
            logger.info("─── Stage 3: 图片语义化 ───")
            _mark_dependency_blocked(progress, "vision", kbd_ids, active_ids)
            # 新架构：VISION 在 IMPORT 之后，仅处理 kbd_entry + kbd_image 已就位的案例
            vision_input_ids = list(active_ids)
            vision_ids = await _get_vision_ready_ids(vision_input_ids, pool)

            t0 = time.monotonic()
            stats = await process_images_batch(vision_ids, pool)
            all_stats["vision"] = {**stats, "elapsed_s": round(time.monotonic() - t0, 1)}
            logger.info("Stage 3 完成 %s", all_stats["vision"])

            # 更新进度（基于 DB images_json desc 完整性判据，详见 _db_vision_status）
            vision_statuses: dict[str, str] = {}
            for cid in vision_input_ids:
                status = await _db_vision_status(pool, cid)
                vision_statuses[cid] = status
                update_stage_status(progress, "vision", cid, status)
            active_ids = [cid for cid in vision_input_ids if vision_statuses[cid] == "done"]
            all_stats["vision"]["blocked_by_dependency"] = len(kbd_ids) - len(vision_input_ids)
            save_progress(run_id, progress)

        if Stage.CLASSIFY in stages:
            logger.info("─── Stage 4: AI 分类 ───")
            _mark_dependency_blocked(progress, "classify", kbd_ids, active_ids)
            classify_input_ids = list(active_ids)
            classify_rows = await pool.fetch(
                """SELECT support_id FROM kbd_entry
                   WHERE support_id = ANY($1)
                     AND status = 'draft'
                     AND (ai_category_id IS NULL OR ai_category_id = '')""",
                classify_input_ids,
            )
            classify_ids_all = [r["support_id"] for r in classify_rows]
            classify_kbd_ids = classify_ids_all

            t0 = time.monotonic()
            stats = await classify_batch(classify_kbd_ids, pool)
            all_stats["classify"] = {**stats, "elapsed_s": round(time.monotonic() - t0, 1)}
            logger.info("Stage 4 完成 %s", all_stats["classify"])

            # 同时验证本次无需调用（已分类）的案例，保持完整的逐 ID 契约。
            classified_ids: list[str] = []
            for cid in classify_input_ids:
                row = await pool.fetchrow(
                    """SELECT ai_category_id FROM kbd_entry WHERE support_id = $1""",
                    cid,
                )
                status = "done" if row and row["ai_category_id"] else "failed"
                update_stage_status(progress, "classify", cid, status)
                if status == "done":
                    classified_ids.append(cid)
            active_ids = classified_ids
            all_stats["classify"]["blocked_by_dependency"] = len(kbd_ids) - len(classify_input_ids)
            save_progress(run_id, progress)

        if Stage.EXTRACT_SIGNALS in stages:
            logger.info("─── Stage 5: 关键信号分级抽取 ───")
            _mark_dependency_blocked(progress, "extract_signals", kbd_ids, active_ids)
            extract_input_ids = list(active_ids)
            # 仅处理已分类且 signals_json 为空的 draft 案例
            extract_rows = await pool.fetch(
                """SELECT support_id FROM kbd_entry
                   WHERE support_id = ANY($1)
                     AND status = 'draft'
                     AND (COALESCE(category_id, '') <> '' OR COALESCE(ai_category_id, '') <> '')
                     AND (signals_json IS NULL OR signals_json = '[]'::jsonb)""",
                extract_input_ids,
            )
            extract_ids_all = [r["support_id"] for r in extract_rows]

            # Resume 模式：跳过已完成的案例
            extract_kbd_ids = extract_ids_all
            if resume and progress:
                completed_ids = get_completed_ids_for_stage(progress, "extract_signals")
                extract_kbd_ids = [cid for cid in extract_ids_all if cid not in completed_ids]
                skipped = len(extract_ids_all) - len(extract_kbd_ids)
                if skipped > 0:
                    logger.info("Resume 跳过 %d 个已完成的 extract 案例", skipped)

            t0 = time.monotonic()
            stats = await extract_signals_batch(extract_kbd_ids, pool)
            all_stats["extract"] = {**stats, "elapsed_s": round(time.monotonic() - t0, 1)}
            logger.info("Stage 5 完成 %s", all_stats["extract"])

            signal_ready_ids: list[str] = []
            for cid in extract_input_ids:
                row = await pool.fetchrow(
                    """SELECT signals_json FROM kbd_entry WHERE support_id = $1""",
                    cid,
                )
                status = _signal_document_status(row["signals_json"] if row else None)
                update_stage_status(progress, "extract_signals", cid, status)
                if status == "done":
                    signal_ready_ids.append(cid)
            active_ids = signal_ready_ids
            all_stats["extract"]["blocked_by_dependency"] = len(kbd_ids) - len(extract_input_ids)
            save_progress(run_id, progress)

        if Stage.AUDIT_LOG_SIGNALS in stages:
            logger.info("─── Stage 6: qfk_log Proposal 只读契约审计 ───")
            _mark_dependency_blocked(progress, "audit_log_signals", kbd_ids, active_ids)
            # 延迟导入，避免只运行 fetch/import 等阶段时强制依赖 backend/shared。
            from .log_signal_audit import audit_rows, load_rows_from_db

            t0 = time.monotonic()
            rows = await load_rows_from_db(pool, active_ids)
            report = audit_rows(rows)
            all_stats["audit_log_signals"] = {
                **report,
                "elapsed_s": round(time.monotonic() - t0, 1),
            }
            logger.info(
                "Stage 6 完成 cases=%d status=%s issues=%s",
                report["case_count"],
                report["case_status_counts"],
                report["issue_counts"],
            )

            case_status = {
                support_id: status
                for status, support_ids in report["case_status_ids"].items()
                for support_id in support_ids
            }
            for support_id in [str(row["support_id"]) for row in rows]:
                audit_status = case_status.get(support_id)
                if audit_status == "BLOCKED_ACTIVE_SIGNAL":
                    progress_status = "failed"
                elif audit_status == "NEEDS_EXPERT_REVIEW":
                    progress_status = "needs_review"
                else:
                    progress_status = "done"
                update_stage_status(progress, "audit_log_signals", support_id, progress_status)
            save_progress(run_id, progress)

        failed_steps = sum(
            1
            for case in progress.get("kbds", {}).values()
            for stage_name in stage_names
            if case.get(stage_name) in {"failed", "needs_review"}
        )
        all_stats["pipeline"] = {
            "success": failed_steps == 0,
            "failed_steps": failed_steps,
            "completed_ids": len(active_ids),
            "total_ids": len(kbd_ids),
        }
        # 标记进度完成
        finish_progress(progress)

    finally:
        await pool.close()
        await http_client.aclose()

    logger.info("流水线全部完成 run_id=%s %s", run_id, all_stats)
    return all_stats, run_id


def _mark_dependency_blocked(
    progress: dict,
    stage: str,
    requested_ids: list[str],
    active_ids: list[str],
) -> None:
    """将前置失败导致未执行的案例明确记为 skipped，避免保留误导性的 pending。"""
    active = set(active_ids)
    for support_id in requested_ids:
        if support_id not in active:
            update_stage_status(progress, stage, support_id, "skipped")


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


def _signal_document_status(raw: object) -> str:
    """Signal 阶段完成判据：非空信号与案例验证契约必须同时存在。"""

    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return "failed"
    if not isinstance(raw, dict):
        return "failed"
    signals = raw.get("signals")
    if not isinstance(signals, list) or not signals:
        return "needs_review"
    if not isinstance(raw.get("verification_contract"), dict):
        return "needs_review"
    return "done"


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

    判据：kbd_entry 存在，且图片缺少 Evidence，或抽取状态属于可重试失败；kbd_image 表非空。

    ``quality.needs_review=true`` 但 ``status=success``（例如物理设备照片被诚实归为
    other）属于人工审核队列，不是自动重试队列。把两者混在一起会造成 failed-only
    永久重跑同一张图，既浪费模型调用，也无法消除真正需要人的语义歧义。

    Args:
        kbd_ids: support_id 列表
        pool: asyncpg 连接池（不传则按 settings.DATABASE_URL 自建）
    """
    if not kbd_ids:
        return []
    close_pool = False
    if pool is None:
        pool = await asyncpg.create_pool(
            dsn=settings.asyncpg_database_url
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
                     OR NOT (img ? 'evidence')
                     OR COALESCE(img->'evidence'->'quality'->>'status', 'success')
                        IN ('partial', 'low_quality', 'failed', 'needs_review')
              )
            """,
            kbd_ids,
        )
        return [r["support_id"] for r in rows]
    finally:
        if close_pool:
            await pool.close()


def _vision_item_status(item: dict) -> str:
    """单图完成判据：兼容 legacy desc，新 Evidence 则以结构化质量为准。"""

    if not (item.get("desc") or "").strip():
        return "failed"
    evidence = item.get("evidence")
    if not isinstance(evidence, dict):
        return "done"
    quality = evidence.get("quality") or {}
    status = str(quality.get("status") or "").lower()
    if status == "failed":
        return "failed"
    if quality.get("needs_review") or status in {"partial", "low_quality", "needs_review"}:
        return "needs_review"
    return "done"


async def _db_vision_status(pool: asyncpg.Pool, support_id: str) -> str:
    """基于 DB images_json Evidence 质量判读 VISION 完成状态。

    判据：
      - kbd_entry 不存在 → 'failed'（异常，前置 IMPORT 失败）
      - images_json 为空 → 'failed'（无图片案例不应进入 VISION 阶段）
      - 存在 seq 但 desc 为空或 Evidence.status=failed → 'failed'
      - Evidence 标记 partial/low_quality/needs_review → 'needs_review'
      - 所有 seq 的结构化质量通过（legacy 数据则 desc 非空）→ 'done'
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
    statuses = [_vision_item_status(item) for item in dict_items]
    if stale_items or "failed" in statuses:
        return "failed"
    if "needs_review" in statuses:
        return "needs_review"
    return "done"


async def run_from_excel(
    stages: Sequence[Stage] = (
        Stage.FETCH,
        Stage.IMPORT,
        Stage.VISION,
        Stage.CLASSIFY,
        Stage.EXTRACT_SIGNALS,
        Stage.AUDIT_LOG_SIGNALS,
    ),
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
