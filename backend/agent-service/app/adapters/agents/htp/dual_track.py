"""双链路编排：按模式选择执行哪条链路，并在并存模式下做结论级融合。

## 编排层是唯一允许同时看见两条链路的地方

- ``kbd_differential``（关键链路）：不知道根因链路存在，函数体零改动；
- ``root_track``（根因链路）：单向依赖关键链路的采集/判定原语；
- ``track_arbiter``（融合）：纯函数，只吃两份已完成结论；
- 本文件（编排）：读模式、决定跑谁、把结果交回调用方。

淘汰任一链路时，只需改本文件的 ``diagnose_with_tracks`` 一处（或直接把模式钉死），
另一条链路的代码与行为完全不受影响。

## 关键链路结论的复算（为什么不改 diagnose）

``KBDDiagnostic.diagnose`` 的 plan / assessments 是函数局部变量，不对外暴露。为了
在**完全不修改关键链路文件**的前提下拿到这两份对象，本模块用同一批 ``StepResult``
重新跑一遍未修改的纯组件（``compile_signal_plan`` → ``initial_assessments`` →
``apply_scope_results`` → 回填逐信号结果 → ``reduce_candidates(finalize=True)``）。
这些组件全部是确定性的，复算结果与 ``diagnose`` 内部那一份逐字等价，而关键链路
文件保持字节不变——「零侵入」因此可以被 git diff 直接举证。
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass
from typing import Any

from shared.cdd import CandidateAssessment, SignalPlan, apply_scope_results, compile_signal_plan
from shared.cdd.candidate_reducer import initial_assessments, reduce_candidates
from shared.cdd.kbd_model import KBD
from shared.observability.logger import get_logger

from app.adapters.agents.htp.kbd_differential import KBDDiagnostic, KBDDiagResult, _tool_contract_checker
from app.adapters.agents.htp.root_display import append_anchors_to_report, build_anchor_entries
from app.adapters.agents.htp.root_track import (
    ROOT_TRACK,
    RootCandidate,
    RootTrackDiagnostic,
    RootTrackResult,
    build_root_candidates,
)
from app.adapters.agents.htp.track_arbiter import (
    MODE_DUAL,
    MODE_PROCESS_ONLY,
    MODE_ROOT_ONLY,
    MODE_SHADOW,
    PROCESS_TRACK,
    FusionOutcome,
    conclusion_tracks,
    fuse_tracks,
    resolve_track_mode,
    runnable_tracks,
)
from app.domain.agent_port import AgentEvent
from app.services.metrics import KBD_CANDIDATE_FUSION_TOTAL

logger = get_logger("kbd-dual-track")


@dataclass
class TrackRunSummary:
    """一次双链路诊断的可观测摘要（赛马评估的最小数据集）。"""

    mode: str
    ran_tracks: tuple[str, ...]
    conclusion_tracks: tuple[str, ...]
    process_level: str | None = None
    root_level: str | None = None
    fused_level: str | None = None
    fused_supported_ids: tuple[str, ...] = ()
    process_supported_ids: tuple[str, ...] = ()
    root_supported_ids: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
    root_reused_acquisitions: int = 0
    root_executed_acquisitions: int = 0
    root_blocked_acquisitions: int = 0
    root_unresolved_requirements: dict[str, list[str]] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "ran_tracks": list(self.ran_tracks),
            "conclusion_tracks": list(self.conclusion_tracks),
            "process_level": self.process_level,
            "root_level": self.root_level,
            "fused_level": self.fused_level,
            "process_supported_ids": list(self.process_supported_ids),
            "root_supported_ids": list(self.root_supported_ids),
            "fused_supported_ids": list(self.fused_supported_ids),
            "conflicts": list(self.conflicts),
            "root_reused_acquisitions": self.root_reused_acquisitions,
            "root_executed_acquisitions": self.root_executed_acquisitions,
            "root_blocked_acquisitions": self.root_blocked_acquisitions,
            "root_unresolved_requirements": self.root_unresolved_requirements or {},
        }


def rebuild_process_track(
    candidates: list[KBD],
    env_context: dict[str, Any],
    steps: list[Any],
    *,
    snapshot_id: str,
) -> tuple[SignalPlan, dict[str, CandidateAssessment]]:
    """用关键链路产出的 StepResult 复算它的 plan 与 assessments。

    纯函数式复算：不读取 ``KBDDiagnostic`` 的任何内部状态，因此不会与关键链路的
    实例状态产生耦合；输入相同则结果与 ``diagnose`` 内部那一份逐字等价。

    只在融合真正需要时才被调用（见 ``diagnose_with_tracks``）。对 ``steps`` 的每个
    元素只做属性探测，无任一必需属性即跳过——编排层不向关键链路索取接口契约，
    关键链路也就不会被本函数的新假设反向约束。
    """
    ordered = sorted(candidates, key=lambda k: (k.support_id or str(k.id), str(k.id)))
    plan = compile_signal_plan(ordered, snapshot_id=snapshot_id, tool_contract_checker=_tool_contract_checker)
    assessments = initial_assessments(plan)
    apply_scope_results(plan, assessments, env_context)
    for step in steps:
        kbd_id = getattr(step, "kbd_id", "")
        signal_id = getattr(step, "signal_id", "")
        outcome = getattr(step, "outcome", None)
        if not kbd_id or not signal_id or outcome is None:
            continue
        assessment = assessments.get(kbd_id)
        if assessment is None:
            continue
        ref_id = next(
            (ref.ref_id for ref in plan.signals.values() if ref.kbd_id == kbd_id and ref.signal_id == signal_id),
            "",
        )
        if ref_id:
            assessment.signal_outcomes[ref_id] = outcome
    # 未出现在 StepResult 里的引用保持 NOT_RUN（与 diagnose 收尾后的状态一致）。
    reduce_candidates(plan, assessments, finalize=True)
    return plan, assessments


def _build_root_engine(engine: KBDDiagnostic) -> RootTrackDiagnostic:
    """用与关键链路相同的依赖构造根因链路实例（两个实例状态互不可见）。

    只用 ``getattr`` 读取依赖，不假设 ``engine`` 一定是真实 ``KBDDiagnostic``：
    测试常用轻量假实现注入，编排层不应因为读一个私有属性就把它打挂。
    """
    return RootTrackDiagnostic(
        ai_registry=getattr(engine, "_ai_registry", None),
        tool_executor=getattr(engine, "_tool_executor", None),
        diagnostic_item_client=getattr(engine, "_diagnostic_item_client", None),
        conversation_id=getattr(engine, "_conversation_id", None),
        case_id=getattr(engine, "_case_id", None),
        assistant_type=getattr(engine, "_assistant_type", "htp-agent"),
        db_session_factory=getattr(engine, "_db_session_factory", None),
    )


def _supported_kbds(plan: SignalPlan | None, kbd_ids: tuple[str, ...]) -> list[KBD]:
    if plan is None:
        return []
    return [plan.candidates[kbd_id] for kbd_id in kbd_ids if kbd_id in plan.candidates]


async def diagnose_with_tracks(
    engine: KBDDiagnostic,
    *,
    candidates: list[KBD],
    root_cases: list[dict[str, Any]],
    env_context: dict[str, Any],
    session_id: str,
    user_id: str = "",
    snapshot_id: str = "runtime",
    mode: str | None = None,
    root_engine_factory: Callable[[], Any] | None = None,
) -> AsyncGenerator[AgentEvent, None]:
    """按 ``KB_SIGNAL_TRACK_MODE`` 执行一条或两条链路，并把最终结果写回 ``engine``。

    :param candidates: 关键链路候选（kb-service 的 ``executable=True`` 集合）
    :param root_cases: 根因链路候选载荷（kb-service 下发的原始 dict，含 ``root_*`` 字段）。
        本层负责把「根因形态」构造出来（``build_root_candidates``），因此调用方与
        共享内核都不需要认识根因信号。
    :param root_engine_factory: 可选根因诊断器工厂（测试注入假实现用；
        默认为根据 ``engine`` 依赖构造真实 ``RootTrackDiagnostic``）
    """
    resolved_mode = resolve_track_mode(mode)
    tracks = runnable_tracks(resolved_mode)
    conclusion = conclusion_tracks(resolved_mode)
    # 只把「根因信号可执行」的候选交给根因链路；text-only 候选仅贡献展示锚点。
    root_candidates: list[RootCandidate] = build_root_candidates(root_cases)
    # 没有可执行根因候选时，根因链路没有可执行内容：直接退化为关键链路单跑。
    # 这是「无根因信号 KBD 行为恒等」的第一道保证：连实例都不构造、不调融合、
    # 不改写 engine._result，关键链路的产出与改造前完全一致。
    root_track_active = ROOT_TRACK in tracks and bool(root_candidates)
    effective_tracks = tuple(track for track in tracks if track != ROOT_TRACK or root_track_active)
    summary = TrackRunSummary(mode=resolved_mode, ran_tracks=effective_tracks, conclusion_tracks=conclusion)
    process_result: KBDDiagResult | None = None
    process_plan: SignalPlan | None = None
    process_assessments: dict[str, CandidateAssessment] | None = None
    root_result: RootTrackResult | None = None

    logger.info(
        event="signal_track_mode",
        message="双链路诊断模式已确定",
        mode=resolved_mode,
        ran_tracks=list(effective_tracks),
        conclusion_tracks=list(conclusion),
        session_id=session_id,
        process_candidate_count=len(candidates),
        root_candidate_count=len(root_candidates),
    )

    # ── 关键链路 ───────────────────────────────────────────────────────────
    if PROCESS_TRACK in effective_tracks:
        async for event in engine.diagnose(
            candidates=candidates,
            env_context=env_context,
            session_id=session_id,
            user_id=user_id,
            snapshot_id=snapshot_id,
        ):
            yield event
        process_result = engine.get_result()
        if process_result is not None:
            summary.process_level = getattr(process_result, "conclusion_level", None)
            summary.process_supported_ids = tuple(
                str(kbd.id) for kbd in getattr(process_result, "matched_kbds", []) or []
            )
            # 只有确实要融合时才复算关键链路：单跑模式下本函数连读都不读
            # steps_executed，关键链路的产出结构因此不构成编排层的隐含契约。
            if root_track_active:
                process_plan, process_assessments = rebuild_process_track(
                    candidates,
                    env_context,
                    list(getattr(process_result, "steps_executed", []) or []),
                    snapshot_id=snapshot_id,
                )

    # ── 根因链路 ───────────────────────────────────────────────────────────
    if root_track_active:
        root_engine = root_engine_factory() if root_engine_factory is not None else _build_root_engine(engine)
        async for event in root_engine.diagnose_root(
            candidates=root_candidates,
            env_context=env_context,
            session_id=session_id,
            user_id=user_id,
            # root_only 由根因链路独占会话证据链；并存时证据链归关键链路所有。
            write_diagnostic_items=resolved_mode == MODE_ROOT_ONLY,
            seed_steps=process_result.steps_executed if process_result else None,
        ):
            yield event
        root_result = root_engine.root_result
        summary.root_reused_acquisitions = root_result.reused_acquisitions
        summary.root_executed_acquisitions = root_result.executed_acquisitions
        summary.root_blocked_acquisitions = root_result.blocked_acquisitions
        summary.root_unresolved_requirements = root_result.unresolved_requirements
        if root_result.decision is not None:
            summary.root_level = root_result.decision.level.value
            summary.root_supported_ids = tuple(root_result.decision.supported_ids)
        if resolved_mode == MODE_ROOT_ONLY:
            # root_only：关键链路没跑，根因链路的结果就是唯一结果。
            engine._result = root_engine.get_result()

    # ── 结论级融合 ─────────────────────────────────────────────────────────
    fusion: FusionOutcome | None = None
    if resolved_mode in (MODE_DUAL, MODE_SHADOW) and root_result is not None and root_result.plan is not None:
        fusion = fuse_tracks(
            mode=resolved_mode,
            process_plan=process_plan,
            process_assessments=process_assessments,
            root_plan=root_result.plan if root_result else None,
            root_assessments=root_result.assessments if root_result else None,
        )
        summary.fused_level = fusion.decision.level.value
        summary.fused_supported_ids = tuple(fusion.decision.supported_ids)
        summary.conflicts = fusion.conflicts
        if resolved_mode == MODE_DUAL and process_result is not None:
            _apply_fused_result(
                engine=engine,
                process_result=process_result,
                fusion=fusion,
                process_plan=process_plan,
                root_result=root_result,
                all_kbds=list(candidates)
                + [item.kbd for item in root_candidates if item.kbd.id not in {c.id for c in candidates}],
            )
        _record_fusion_metrics(fusion)
    elif resolved_mode == MODE_PROCESS_ONLY and process_result is not None:
        # 纯关键链路模式：只在报告尾部保留根因 text 锚点为空（根因链路未运行）。
        pass

    # text 锚点是根因链路的展示能力，由本层统一追加（关键链路文件不含该逻辑）。
    # ``process_only`` 表示根因链路整体未运行，此时不追加任何根因内容；
    # 其余模式（dual/shadow/root_only）都在「当前有效结果」上追加。
    anchor_entries = build_anchor_entries(root_cases)
    if resolved_mode != MODE_PROCESS_ONLY and anchor_entries:
        target = engine.get_result()
        if target is not None:
            merged_report = append_anchors_to_report(target.diagnosis_report, anchor_entries)
            if merged_report != target.diagnosis_report:
                target.diagnosis_report = merged_report

    logger.info(
        event="signal_track_summary",
        message="双链路诊断完成（赛马度量）",
        session_id=session_id,
        snapshot_id=snapshot_id,
        **summary.as_dict(),
        fusion_details=fusion.detail_map() if fusion else {},
    )
    if summary.conflicts:
        logger.warning(
            event="signal_track_conflict",
            message="两条链路给出相反结论（同一候选一路支持一路拒绝）",
            session_id=session_id,
            conflicts=list(summary.conflicts),
        )


def _apply_fused_result(
    *,
    engine: KBDDiagnostic,
    process_result: KBDDiagResult,
    fusion: FusionOutcome,
    process_plan: SignalPlan | None,
    root_result: RootTrackResult | None,
    all_kbds: list[KBD],
) -> None:
    """把融合结论写回统一结果入口（仅 dual 模式）。

    报告正文沿用关键链路（用户可见的证据链属于实际采集方），融合差异由
    ``_fused_report_section`` 确定性追加，不触发额外的 LLM 调用。
    """
    by_id = {str(kbd.id): kbd for kbd in all_kbds}
    fused_supported = [by_id[kbd_id] for kbd_id in fusion.decision.supported_ids if kbd_id in by_id]
    section = _fused_report_section(fusion, process_result)
    report = process_result.diagnosis_report + section if section else process_result.diagnosis_report
    engine._result = KBDDiagResult(
        matched_kbds=fused_supported,
        steps_executed=process_result.steps_executed,
        is_definitive=fusion.decision.level.value == "definitive",
        diagnosis_report=report,
        conclusion_level=fusion.decision.level.value,
        candidate_states={kbd_id: item.state.value for kbd_id, item in fusion.assessments.items()},
    )


def _fused_report_section(fusion: FusionOutcome, process_result: KBDDiagResult) -> str:
    """融合结论与关键链路结论不一致时，追加确定性的差异说明。"""
    process_supported = {str(kbd.id) for kbd in process_result.matched_kbds}
    fused_supported = set(fusion.decision.supported_ids)
    gained = sorted(fused_supported - process_supported)
    lost = sorted(process_supported - fused_supported)
    if not gained and not lost and not fusion.conflicts:
        return ""
    lines = [
        "",
        "",
        "**根因信号链路（并行策略）核对结果**：",
        f"- 融合结论：{fusion.decision.level.value}",
    ]
    if gained:
        lines.append(f"- 仅根因信号链路支持：{', '.join(gained)}")
    if lost:
        lines.append(f"- 仅关键信号链路支持：{', '.join(lost)}")
    if fusion.conflicts:
        lines.append(f"- 两路结论相反（需人工复核）：{', '.join(fusion.conflicts)}")
    lines.append("- 说明：两条链路各自独立取证，以上差异用于策略赛马评估，不代表现场证据互相否定。")
    return "\n".join(lines)


def _record_fusion_metrics(fusion: FusionOutcome) -> None:
    """按（关键路状态, 根因路状态）记录融合分布，用于赛马收益度量。"""
    try:
        for detail in fusion.details.values():
            KBD_CANDIDATE_FUSION_TOTAL.labels(
                fusion_mode=fusion.mode,
                state=detail.merged_state,
                track_states=f"{PROCESS_TRACK}={detail.process_state}+{ROOT_TRACK}={detail.root_state}",
                active_tracks="+".join(conclusion_tracks(fusion.mode)),
            ).inc()
    except Exception as exc:
        logger.warning(event="kbd_fusion_metrics_failed", error=str(exc))


def fusion_evidence(fusion: FusionOutcome | None) -> str:
    """把融合明细序列化为单行 JSON，便于写入日志/工单上下文做长期追溯。"""
    if fusion is None:
        return ""
    return json.dumps(fusion.detail_map(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
