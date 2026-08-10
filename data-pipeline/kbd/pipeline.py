"""
data-pipeline/kbd/pipeline.py — KBD 知识生产管道编排（API 调用版）

完整流水线分六个 Stage：

  Stage 1: fetch      抓取 API + 下载图片 → 文件存储（cache/{support_id}/）
  Stage 2: import     语义转换 + 原子写入 kbd_entry/kbd_image（status=draft）
  Stage 3: classify   AI 分类（调用 kb-service API）→ kbd_entry.ai_category_id
  Stage 4: vision     图片语义化（Vision LLM）→ 写 kbd_entry.images_json
  Stage 5: extract    关键信号抽取（调用 kb-service API）→ kbd_entry.signals_json
  Stage 6: review     基于 Shared Resolution Runtime 审查全部 Signal

变更（T2-02, T2-03）：
  - Stage 2: 不再直接写数据库，改为调用 `/api/kbd/ingest`
  - Stage 3: 不再本地调用 LLM/直写分类列，改为调用
    `/api/admin/kbd/{id}/reclassify` 原子更新主记录并追加统一 Proposal revision

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
  python -m kbd.run review-signals --all
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import unicodedata
from collections.abc import Iterable, Sequence
from enum import IntEnum

import asyncpg
import httpx

from .classifier import classify_batch
from .config import settings
from .extract_signals import extract_signals_batch
from .fetcher import _is_fetched, fetch_batch, read_ids_from_excel
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
from .terminal_layout import TERMINAL_LAYOUT_WIDTH

logger = logging.getLogger("kbd.pipeline")


_STAGE_BANNER_WIDTH = TERMINAL_LAYOUT_WIDTH


def _annotate_stage_scope(
    stats: dict,
    *,
    candidate_ids: Iterable[str],
    selected_ids: Iterable[str],
    blocked: int = 0,
    no_work_reason: str = "已有结果",
) -> dict:
    """把“计划范围”和“实际结果”分开记录，避免全 0 被误读为成功。

    ``done/failed/skipped`` 是业务结果计数；它们不能表达任务管理器是否
    根本没有把案例交给该 Stage。CLI 因此额外输出候选数、选中数和未安排数。
    """

    candidates = list(candidate_ids)
    selected = list(selected_ids)
    stats["candidate_cases"] = len(candidates)
    stats["selected_cases"] = len(selected)
    stats["not_scheduled"] = max(0, len(candidates) - len(selected))
    stats["blocked_by_dependency"] = int(stats.get("blocked_by_dependency", blocked) or blocked)
    if not selected and stats["blocked_by_dependency"] > 0:
        stats["execution_status"] = "blocked"
        stats["execution_reason"] = "前置依赖未满足"
    elif not selected and stats["blocked_by_dependency"] == 0:
        # 已有持久化结果（尤其是幂等 Classify）是“无需执行”，而不是“未安排”。
        result_count = sum(
            int(stats.get(key, 0) or 0)
            for key in ("done", "created", "overridden", "skipped", "case_count")
        )
        if result_count:
            stats["execution_status"] = "no_work"
            stats["execution_reason"] = "已有结果"
        elif candidates:
            stats["execution_status"] = "no_work"
            stats["execution_reason"] = no_work_reason
        else:
            stats["execution_status"] = "not_scheduled"
            stats["execution_reason"] = "没有可执行 KBD 候选（前置阶段未提供输入）"
    else:
        stats["execution_status"] = "executed"
    return stats


def _log_stage_result(stage_number: int, stats: dict) -> None:
    """以操作者可理解的语言记录阶段结论，而不是只倾倒一个字典。"""

    status = stats.get("execution_status")
    if status == "not_scheduled":
        logger.info(
            "Stage %d 未执行：任务计划未选择案例 candidate=%d selected=0 reason=%s",
            stage_number,
            stats.get("candidate_cases", 0),
            stats.get("execution_reason", "-"),
        )
    elif status == "no_work":
        logger.info(
            "Stage %d 无需执行：%s",
            stage_number,
            stats.get("execution_reason", "-"),
        )
    elif status == "blocked":
        logger.warning(
            "Stage %d 未执行：前置依赖阻断 blocked=%d candidate=%d selected=0",
            stage_number,
            stats.get("blocked_by_dependency", 0),
            stats.get("candidate_cases", 0),
        )
    else:
        if stage_number == 2:
            done = sum(int(stats.get(key, 0) or 0) for key in ("created", "overridden"))
            failed = int(stats.get("error", 0) or 0)
        elif stage_number == 6:
            done = int(stats.get("case_count", 0) or 0)
            failed = int(stats.get("case_status_counts", {}).get("BLOCKED_SIGNAL_REVIEW", 0) or 0)
        else:
            done = int(stats.get("done", 0) or 0)
            failed = int(stats.get("failed", 0) or 0)
        needs_review = int(stats.get("needs_review", 0) or 0)
        if not needs_review:
            needs_review = int(stats.get("case_status_counts", {}).get("needs_review", 0) or 0)
        logger.info(
            "Stage %d 完成：selected=%d done=%d failed=%d skipped=%d needs_review=%d blocked=%d elapsed=%ss",
            stage_number,
            stats.get("selected_cases", 0),
            done,
            failed,
            stats.get("skipped", 0),
            needs_review,
            stats.get("blocked_by_dependency", 0),
            stats.get("elapsed_s", "-"),
        )

# 空信号文档的兼容判据。
# 正常 Import 应写入 []，但历史/不同版本的写入路径可能留下 {}，v2 文档也可能
# 已经存在但 signals 为空。三种形态都表示“尚未生成 Proposal”，必须允许 Stage 5
# 重新抽取；包含实际 signals 的对象不能命中此条件。
EMPTY_SIGNALS_JSON_PREDICATE = """(
    signals_json IS NULL
    OR signals_json IN ('[]'::jsonb, '{}'::jsonb)
    OR (
        jsonb_typeof(signals_json) = 'object'
        AND (
            NOT (signals_json ? 'signals')
            OR (
                jsonb_typeof(signals_json->'signals') = 'array'
                AND jsonb_array_length(signals_json->'signals') = 0
            )
        )
    )
)"""


def _display_width(text: str) -> int:
    """计算中英文混排的终端显示宽度。"""

    return sum(2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1 for char in text)


def _stage_banner(stage: int, title: str, *, width: int = _STAGE_BANNER_WIDTH) -> str:
    """生成固定总宽度、标题居中的阶段分隔线。

    日志终端通常按显示列宽渲染中文字符（一个中文字符占两列），不能直接用
    ``len`` 或固定数量的左右 ``=``。这里先计算标题的显示宽度，再把剩余列
    平分到两侧，保证所有 Stage 的首尾严格对齐。
    """

    label = f"  Stage {stage}: {title}  "
    remaining = max(0, width - _display_width(label))
    left = remaining // 2
    right = remaining - left
    return f"{'=' * left}{label}{'=' * right}"


class Stage(IntEnum):
    """流水线阶段枚举。

    新架构 DAG（有向无环，依赖单向）：
      FETCH → IMPORT → {CLASSIFY ∥ VISION} → EXTRACT_SIGNALS → REVIEW_SIGNALS
    VISION 移到 IMPORT 之后（因为 VISION 需要 kbd_entry.id 和 kbd_image），
    IMPORT 原子写入 kbd_entry + kbd_image，消除循环依赖。
    """
    FETCH = 1
    IMPORT = 2  # 原子写入 kbd_entry + kbd_image
    CLASSIFY = 3  # 调 kb-service API，更新 ai_category_id
    VISION = 4  # 读 kbd_image，调 Vision LLM，更新 images_json + rebuild content_md
    EXTRACT_SIGNALS = 5  # 关键信号分级抽取：LLM 抽取 signals_json 并写回 kbd_entry
    REVIEW_SIGNALS = 6  # 只读审查：所有 Signal 必须经过 Shared Resolution Runtime


# ─── DAG 依赖声明（拓扑排序 P0-② 增强）─────────────────────────────────────────

# Stage DAG：每个 stage 声明其所有硬前置依赖（直接前置，闭包由 resolve_stages 自动展开）。
# VISION 与 CLASSIFY 都只依赖 IMPORT；EXTRACT_SIGNALS 同时要求两者成功，图片证据
# 是关键信号抽取的业务硬依赖，不能因“任务已结束但识图失败”而降级放行。
STAGE_DEPENDENCIES: dict[Stage, tuple[Stage, ...]] = {
    Stage.FETCH: (),                                                  # 无前置
    Stage.IMPORT: (Stage.FETCH,),                                     # 需要 raw.json + 本地图片
    Stage.CLASSIFY: (Stage.IMPORT,),                                  # 结构化文本字段已由 IMPORT 写入
    Stage.VISION: (Stage.IMPORT,),                                    # 需要 kbd_entry + kbd_image
    Stage.EXTRACT_SIGNALS: (Stage.VISION, Stage.CLASSIFY),             # 图片证据 + 领域分类都是硬依赖
    Stage.REVIEW_SIGNALS: (Stage.EXTRACT_SIGNALS,),                    # 审查生产后的 signals_json
}


def resolve_stages(requested: Iterable[Stage]) -> list[Stage]:
    """DAG 闭包 + 拓扑序展开。

    自动补齐用户请求 stage 的所有前置依赖（如只跑 VISION，自动拉取 IMPORT + FETCH）。
    解决"只跑 VISION 但缺少前置导致 pipeline 静默失败"的边界问题。

    Args:
        requested: 用户传入的 stages（含去重）

    Returns:
        按拓扑序（FETCH → IMPORT → CLASSIFY/VISION → EXTRACT → REVIEW）排列的全部 stage 列表，
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
    # 按 Stage 枚举值排序（FETCH=1 → REVIEW_SIGNALS=6，天然拓扑序）
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
        Stage.CLASSIFY,
        Stage.VISION,
        Stage.EXTRACT_SIGNALS,
        Stage.REVIEW_SIGNALS,
    ),
    *,
    force_fetch: bool = False,
    override: bool = False,
    override_status: list[str] | None = None,
    resume: bool = False,
    resume_run_id: str | None = None,
    failed_only: bool = False,
    run_id: str | None = None,
    task_ids_by_stage: dict[Stage, list[str]] | None = None,
    task_mode: str = "legacy",
    rework_statuses: list[str] | None = None,
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
        task_ids_by_stage: 统一任务管理器为每个 Stage 选出的任务 ID。
        task_mode: 任务模式（default/resume/failed/rework），仅用于日志和重做策略。
        rework_statuses: 重做时允许的 KBD 状态；默认由 task 命令传入 draft。

    Returns:
        (各 stage 的统计结果, 实际使用的 run_id)
    """
    if not kbd_ids:
        logger.warning("kbd_ids 为空，流水线退出")
        return {}, run_id or generate_run_id()

    planned_ids = {
        stage: set(ids)
        for stage, ids in (task_ids_by_stage or {}).items()
    }

    def _planned(stage: Stage, ids: Iterable[str]) -> list[str]:
        """返回统一任务计划允许本 Stage 执行的 ID。"""

        if task_ids_by_stage is None:
            return list(ids)
        allowed = planned_ids.get(stage, set())
        return [support_id for support_id in ids if support_id in allowed]

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

    # ── 提前创建连接池（各 stage 都需要使用）──
    pool = await _create_pool()

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
    rework = task_mode == "rework"
    no_work_reason_by_mode = {
        "resume": "当前断点续跑模式无需处理",
        "failed": "当前失败重试模式无需处理",
    }
    no_work_reason = no_work_reason_by_mode.get(task_mode, "已有结果")

    async def _existing_ready(stage: Stage, ids: Iterable[str]) -> list[str]:
        """Return IDs whose persisted output satisfies a stage dependency."""
        candidates = list(ids)
        if stage is Stage.FETCH:
            return [cid for cid in candidates if _is_fetched(cid)]
        if stage is Stage.IMPORT:
            ready: list[str] = []
            for cid in candidates:
                row = await pool.fetchrow(
                    "SELECT id FROM kbd_entry WHERE support_id = $1", cid
                )
                if row:
                    ready.append(cid)
            return ready
        if stage is Stage.CLASSIFY:
            rows = await pool.fetch(
                "SELECT support_id FROM kbd_entry WHERE support_id = ANY($1) AND COALESCE(ai_category_id, '') <> ''",
                candidates,
            )
            return [str(row["support_id"]) for row in rows]
        if stage is Stage.VISION:
            ready: list[str] = []
            for cid in candidates:
                if await _db_vision_status(pool, cid) in {"done", "no_images"}:
                    ready.append(cid)
            return ready
        if stage is Stage.EXTRACT_SIGNALS:
            rows = await pool.fetch(
                f"SELECT support_id, signals_json FROM kbd_entry WHERE support_id = ANY($1) AND {EMPTY_SIGNALS_JSON_PREDICATE} IS NOT TRUE",
                candidates,
            )
            return [str(row["support_id"]) for row in rows if _signal_document_status(row["signals_json"]) == "done"]
        return candidates

    async def _status_scoped(stage: Stage, ids: Iterable[str]) -> list[str]:
        """在已有 KBD 的阶段应用 rework 状态范围；Fetch 尚无 KBD 状态。"""
        candidates = list(ids)
        if not rework or stage is Stage.FETCH or not candidates:
            return candidates
        rows = await pool.fetch(
            "SELECT support_id FROM kbd_entry WHERE support_id = ANY($1) AND status = ANY($2)",
            candidates,
            list(rework_statuses or ["draft"]),
        )
        allowed = {str(row["support_id"]) for row in rows}
        return [cid for cid in candidates if cid in allowed]

    try:
        if Stage.FETCH in stages:
            logger.info(_stage_banner(1, "数据抓取"))
            fetch_ids = _planned(Stage.FETCH, kbd_ids)
            t0 = time.monotonic()
            stats = await fetch_batch(
                fetch_ids,
                force=force_fetch or rework,
                retry_images=(task_mode != "resume"),
            )
            all_stats["fetch"] = {**stats, "elapsed_s": round(time.monotonic() - t0, 1)}
            _annotate_stage_scope(
                all_stats["fetch"], candidate_ids=kbd_ids, selected_ids=fetch_ids,
                no_work_reason=no_work_reason,
            )
            _log_stage_result(1, all_stats["fetch"])

            # 更新进度
            for cid in fetch_ids:
                status = "done" if _is_fetched(cid) else "failed"
                update_stage_status(progress, "fetch", cid, status)
            active_ids = await _existing_ready(Stage.FETCH, kbd_ids)
            save_progress(run_id, progress)

        if Stage.IMPORT in stages:
            logger.info(_stage_banner(2, "语义提取 + 原子入库"))
            _mark_dependency_blocked(progress, "import", kbd_ids, active_ids)
            ready_ids = await _get_import_ready_ids(active_ids, pool)
            import_ids = _planned(Stage.IMPORT, await _status_scoped(Stage.IMPORT, ready_ids))

            t0 = time.monotonic()
            stats = await import_batch(
                import_ids,
                pool,
                override=override or rework,
                override_status=override_status or (rework_statuses or ["draft"]),
                client=http_client,
            )
            all_stats["import"] = {**stats, "elapsed_s": round(time.monotonic() - t0, 1)}
            _annotate_stage_scope(
                all_stats["import"], candidate_ids=ready_ids, selected_ids=import_ids,
                no_work_reason=no_work_reason,
            )

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
            successful_current = {
                cid for cid in import_ids
                if import_results.get(cid) in successful_import_statuses
            }
            # 已经成功的依赖可以作为 ready 继续向下游传播；本次选中但失败的
            # Import 即使数据库残留旧行，也必须被阻断，不能被历史数据“救活”。
            persisted_ready = await _existing_ready(
                Stage.IMPORT,
                [cid for cid in ready_ids if cid not in set(import_ids)],
            )
            active_ids = list(dict.fromkeys([*successful_current, *persisted_ready]))
            # ``import_ids`` 还会受到本次任务计划/生命周期模式限制；它为空
            # 不代表依赖阻断。只有进度账本中明确标记为 blocked 的案例才计数。
            all_stats["import"]["blocked_by_dependency"] = _blocked_count(
                progress, "import"
            )
            # 依赖阻断是在批量调用后才能确定；刷新一次面向 CLI 的执行结论。
            _annotate_stage_scope(
                all_stats["import"], candidate_ids=ready_ids, selected_ids=import_ids,
                blocked=all_stats["import"]["blocked_by_dependency"],
                no_work_reason=no_work_reason,
            )
            _log_stage_result(2, all_stats["import"])
            save_progress(run_id, progress)

        # VISION 与 CLASSIFY 没有技术前置关系：都只读取 IMPORT 写入的结构化 KBD。
        # 但它们会争用同一 Provider，因此后端的 LLM_GLOBAL_CONCURRENCY 才是唯一并发边界。
        # EXTRACT_SIGNALS 则必须等待两者成功；图片包含关键诊断信息，禁止降级跳过。
        vision_done_ids: set[str] | None = None
        classified_ids: set[str] | None = None

        async def _run_vision_stage(input_ids: list[str]) -> set[str]:
            _mark_dependency_blocked(progress, "vision", kbd_ids, input_ids)
            vision_candidates = await _get_vision_ready_ids(input_ids, pool)
            vision_ids = _planned(Stage.VISION, await _status_scoped(Stage.VISION, vision_candidates))
            started_at = time.monotonic()
            stats = await process_images_batch(vision_ids, pool, rework=rework)
            all_stats["vision"] = {**stats, "elapsed_s": round(time.monotonic() - started_at, 1)}
            stats.setdefault("case_results", {})

            completed: set[str] = set()
            status_counts: dict[str, int] = {
                "done": 0,
                "failed": 0,
                "needs_review": 0,
                "no_images": 0,
            }
            inconsistent_ids: list[str] = []
            api_case_results = stats.get("case_results", {})
            for cid in input_ids:
                # DB 是 images_json 的最终事实源；同时记录 API 图片级统计与案例级状态
                # 的矛盾，避免再次把“31 张图完成”显示成“9 个 KBD 不明失败”。
                status = await _db_vision_status(pool, cid)
                api_status = (api_case_results.get(cid) or {}).get("status")
                if api_status == "done" and status != "done":
                    # API 图片级 Job 已报告成功而持久化结果不满足案例级契约：这是数据
                    # 一致性故障，不允许静默转成普通 Vision 失败。
                    inconsistent_ids.append(cid)
                    logger.error(
                        "Vision 状态不一致 support_id=%s api_status=%s db_status=%s",
                        cid, api_status, status,
                        extra={"support_id": cid, "stage": "vision", "error_code": "VISION_STATE_INCONSISTENT"},
                    )
                status_counts[status] = status_counts.get(status, 0) + 1
                if cid in vision_ids:
                    # no_images 是 Vision 的观测细分，但对 DAG 来说是可继续的终态，
                    # 因此进度账本仍记录 done，避免下次任务选择器重复调度它。
                    update_stage_status(
                        progress,
                        "vision",
                        cid,
                        "done" if status == "no_images" else status,
                    )
                if status == "no_images":
                    # 无图是合法的终态：允许下游继续，但在 Vision 统计中明确标记为跳过，
                    # 不把它伪装成一次成功的图片识别。
                    stats["skipped"] = int(stats.get("skipped", 0)) + 1
                    # all_stats 是在 API 调用后做的浅拷贝，整数不会随 stats 自动同步。
                    all_stats["vision"]["skipped"] = stats["skipped"]
                    stats["case_results"][cid] = {
                        "status": "skipped",
                        "reason": "案例没有可识别图片",
                    }
                    completed.add(cid)
                elif status == "done":
                    completed.add(cid)
            all_stats["vision"].update(
                case_status_counts=status_counts,
                state_inconsistent_ids=inconsistent_ids,
                blocked_by_dependency=_blocked_count(progress, "vision"),
                item_states={},
            )
            _annotate_stage_scope(
                all_stats["vision"], candidate_ids=input_ids, selected_ids=vision_ids,
                blocked=all_stats["vision"]["blocked_by_dependency"],
                no_work_reason=no_work_reason,
            )
            for cid in vision_ids:
                row = await pool.fetchrow(
                    "SELECT images_json FROM kbd_entry WHERE support_id = $1", cid
                )
                images = row["images_json"] if row else []
                if isinstance(images, str):
                    try:
                        images = json.loads(images)
                    except json.JSONDecodeError:
                        images = []
                if isinstance(images, list):
                    all_stats["vision"]["item_states"][cid] = {
                        str(item.get("seq", index)): {
                            "executed": True,
                            "success": _vision_item_status(item) == "done",
                            "rework": rework,
                        }
                        for index, item in enumerate(images)
                        if isinstance(item, dict)
                    }
            _log_stage_result(4, all_stats["vision"])
            # 未选中的案例沿用已持久化的成功结果，供下游依赖使用。
            persisted = await _existing_ready(Stage.VISION, input_ids)
            return set(persisted) | completed

        async def _run_classify_stage(input_ids: list[str]) -> set[str]:
            _mark_dependency_blocked(progress, "classify", kbd_ids, input_ids)
            started_at = time.monotonic()
            classify_ids = _planned(Stage.CLASSIFY, await _status_scoped(Stage.CLASSIFY, input_ids))
            stats = await classify_batch(classify_ids, pool, rework=rework)
            all_stats["classify"] = {**stats, "elapsed_s": round(time.monotonic() - started_at, 1)}

            completed: set[str] = set()
            for cid in classify_ids:
                row = await pool.fetchrow(
                    """SELECT ai_category_id FROM kbd_entry WHERE support_id = $1""", cid
                )
                status = "done" if row and row["ai_category_id"] else "failed"
                update_stage_status(progress, "classify", cid, status)
                if status == "done":
                    completed.add(cid)
            # 阶段统计按案例最终状态计数，而不是只统计本轮实际调用 API 的数量。
            # 这样幂等重跑时，数据库中已有分类的案例也会正确显示 done=1。
            persisted = await _existing_ready(Stage.CLASSIFY, input_ids)
            completed.update(persisted)
            all_stats["classify"]["done"] = len(completed)
            all_stats["classify"]["failed"] = len(classify_ids) - len(completed & set(classify_ids))
            all_stats["classify"]["blocked_by_dependency"] = _blocked_count(
                progress, "classify"
            )
            _annotate_stage_scope(
                all_stats["classify"], candidate_ids=input_ids, selected_ids=classify_ids,
                blocked=all_stats["classify"]["blocked_by_dependency"],
                no_work_reason=no_work_reason,
            )
            _log_stage_result(3, all_stats["classify"])
            return completed

        if Stage.VISION in stages and Stage.CLASSIFY in stages:
            # 两个阶段的输入快照必须相同；禁止 Vision 的失败缩小分类覆盖面。
            # 两个协程的日志必然按实际完成时序交错，因此使用一个共同 Banner，避免
            # 连续打印两个独立阶段标题后让操作者误以为后续日志只属于 Stage 4。
            logger.info(_stage_banner(3, "AI 分类 + Stage 4: 图片语义化"))
            vision_done_ids, classified_ids = await asyncio.gather(
                _run_vision_stage(list(active_ids)),
                _run_classify_stage(list(active_ids)),
            )
            save_progress(run_id, progress)
        elif Stage.VISION in stages:
            logger.info(_stage_banner(4, "图片语义化"))
            vision_done_ids = await _run_vision_stage(list(active_ids))
            active_ids = list(vision_done_ids)
            save_progress(run_id, progress)
        elif Stage.CLASSIFY in stages:
            logger.info(_stage_banner(3, "AI 分类"))
            classified_ids = await _run_classify_stage(list(active_ids))
            active_ids = list(classified_ids)
            save_progress(run_id, progress)

        if vision_done_ids is not None and classified_ids is not None:
            # EXTRACT 的硬依赖交集：不允许“分类已完成但截图失败”的 KBD 进入 LLM 抽取。
            active_ids = sorted(vision_done_ids & classified_ids)

        if Stage.EXTRACT_SIGNALS in stages:
            logger.info(_stage_banner(5, "关键信号分级抽取"))
            _mark_dependency_blocked(progress, "extract_signals", kbd_ids, active_ids)
            extract_input_ids = list(active_ids)
            # 正常/失败重试只抽取空文档；rework 显式允许重做已有 Proposal。
            status_values = list(rework_statuses or ["draft"]) if rework else ["draft"]
            status_predicate = "status = ANY($2)"
            signal_predicate = "TRUE" if rework else EMPTY_SIGNALS_JSON_PREDICATE
            extract_rows = await pool.fetch(
                f"""SELECT support_id FROM kbd_entry
                   WHERE support_id = ANY($1)
                     AND {status_predicate}
                     AND (COALESCE(category_id, '') <> '' OR COALESCE(ai_category_id, '') <> '')
                     AND {signal_predicate}""",
                extract_input_ids,
                status_values,
            )
            extract_ids_all = [r["support_id"] for r in extract_rows]

            if not extract_ids_all and extract_input_ids:
                logger.info(
                    "Stage 5 无可抽取输入：候选案例均已有非空 signals_json，或不满足 draft/分类前置条件 "
                    "active=%d",
                    len(extract_input_ids),
                )

            # Resume 模式：跳过已完成的案例
            extract_kbd_ids = _planned(Stage.EXTRACT_SIGNALS, extract_ids_all)
            if resume and progress:
                completed_ids = get_completed_ids_for_stage(progress, "extract_signals")
                extract_kbd_ids = [cid for cid in extract_ids_all if cid not in completed_ids]
                skipped = len(extract_ids_all) - len(extract_kbd_ids)
                if skipped > 0:
                    logger.info("Resume 跳过 %d 个已完成的 extract 案例", skipped)

            t0 = time.monotonic()
            stats = await extract_signals_batch(extract_kbd_ids, pool, rework=rework)
            all_stats["extract"] = {**stats, "elapsed_s": round(time.monotonic() - t0, 1)}
            _annotate_stage_scope(
                all_stats["extract"], candidate_ids=extract_input_ids, selected_ids=extract_kbd_ids,
                no_work_reason=no_work_reason,
            )

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
            all_stats["extract"]["blocked_by_dependency"] = _blocked_count(
                progress, "extract_signals"
            )
            _annotate_stage_scope(
                all_stats["extract"], candidate_ids=extract_input_ids, selected_ids=extract_kbd_ids,
                blocked=all_stats["extract"]["blocked_by_dependency"],
                no_work_reason=no_work_reason,
            )
            _log_stage_result(5, all_stats["extract"])
            save_progress(run_id, progress)

        if Stage.REVIEW_SIGNALS in stages:
            logger.info(_stage_banner(6, "Shared Runtime 全量 Signal 审查"))
            _mark_dependency_blocked(progress, "review_signals", kbd_ids, active_ids)
            # 延迟导入，避免只运行 fetch/import 等阶段时强制依赖 backend/shared。
            from .signal_review import load_rows_from_db, review_rows

            t0 = time.monotonic()
            rows = await load_rows_from_db(pool, active_ids)
            report = review_rows(rows)
            all_stats["review_signals"] = {
                **report,
                "elapsed_s": round(time.monotonic() - t0, 1),
            }
            _annotate_stage_scope(
                all_stats["review_signals"],
                candidate_ids=active_ids,
                selected_ids=[str(row["support_id"]) for row in rows],
                no_work_reason=no_work_reason,
            )
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
                review_status = case_status.get(support_id)
                if review_status == "BLOCKED_SIGNAL_REVIEW":
                    progress_status = "failed"
                elif review_status == "NEEDS_SIGNAL_REVIEW":
                    progress_status = "needs_review"
                else:
                    progress_status = "done"
                update_stage_status(progress, "review_signals", support_id, progress_status)
            save_progress(run_id, progress)

        failed_steps = sum(
            1
            for case in progress.get("kbds", {}).values()
            for stage_name in stage_names
            if case.get(stage_name) in {"failed", "blocked_by_dependency"}
        )
        warning_steps = sum(
            1
            for case in progress.get("kbds", {}).values()
            for stage_name in stage_names
            if case.get(stage_name) in {"needs_review", "warning"}
        )
        all_stats["pipeline"] = {
            "success": failed_steps == 0,
            "failed_steps": failed_steps,
            "warning_steps": warning_steps,
            "completed_ids": len(active_ids),
            "total_ids": len(kbd_ids),
        }
        # 标记进度完成
        finish_progress(progress)

    finally:
        await pool.close()
        await http_client.aclose()

    pipeline_result = all_stats.get("pipeline", {})
    logger.info(
        "流水线运行结束 run_id=%s success=%s completed=%d/%d failed_steps=%d warning_steps=%d",
        run_id,
        pipeline_result.get("success", False),
        pipeline_result.get("completed_ids", 0),
        pipeline_result.get("total_ids", len(kbd_ids)),
        pipeline_result.get("failed_steps", 0),
        pipeline_result.get("warning_steps", 0),
    )
    logger.debug("流水线详细统计 run_id=%s stats=%s", run_id, all_stats)
    return all_stats, run_id


def _mark_dependency_blocked(
    progress: dict,
    stage: str,
    requested_ids: list[str],
    active_ids: list[str],
) -> None:
    """明确标记硬依赖阻断，绝不把“未执行”伪装为幂等跳过。"""
    active = set(active_ids)
    for support_id in requested_ids:
        if support_id not in active:
            update_stage_status(progress, stage, support_id, "blocked_by_dependency")


def _blocked_count(progress: dict, stage: str) -> int:
    """统计真正因前置依赖未满足而阻断的案例数。

    未被本次任务计划选中，或复用了数据库中已有成功结果，都不属于依赖
    阻断；只有 ``_mark_dependency_blocked`` 写入账本的状态才计入。
    """

    return sum(
        1
        for case in progress.get("kbds", {}).values()
        if case.get(stage) == "blocked_by_dependency"
    )


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
      - kbd_image 数量为 0 → 'no_images'（合法终态，不调用 Vision）
      - 有图片但 images_json 为空 → 'failed'
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
    # asyncpg 默认把 json/jsonb 解码为 str（除非连接显式注册 JSON codec）。
    # 不能直接遍历该字符串，否则每个字符都会被当成非 dict 的 legacy 条目，导致
    # API 明明报告全部图片完成，DB 复核却把所有正常案例误判为 failed。
    if isinstance(images_json, str):
        try:
            images_json = json.loads(images_json)
        except json.JSONDecodeError:
            return "failed"
    img_count = row["img_count"]
    # 无图片案例是合法终态：不需要调用 Vision，但必须与“图片识别成功”区分，
    # 这样批量日志和案例级状态不会产生误导。
    if img_count == 0:
        return "no_images"
    if not isinstance(images_json, list) or not images_json:
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
        Stage.REVIEW_SIGNALS,
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
