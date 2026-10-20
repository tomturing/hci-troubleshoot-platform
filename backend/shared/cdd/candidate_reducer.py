"""将信号证据确定性归并为 KBD 候选状态。

v2：按证据路径（track）分路归约后再晚融合。关键信号走 ``process``，可执行根因信号走
``root``；两路各自计算 must/should/exclude，互不影响（见 fusion.py 的设计约束）。

兼容性：既有 KBD 全部只有 process 信号，此时 ``track_states`` 只含一项，融合结果与
改造前的单路归约逐字等价，既有调用方与测试零改动。
"""

from __future__ import annotations

from .fusion import FUSION_ROOT_OR_PROCESS, PROCESS_TRACK, fuse_track_states
from .models import CandidateAssessment, CandidateState, EvidenceRole, SignalOutcome, SignalPlan


def initial_assessments(plan: SignalPlan) -> dict[str, CandidateAssessment]:
    assessments: dict[str, CandidateAssessment] = {}
    for kbd_id in plan.candidates:
        if kbd_id in plan.compile_errors:
            assessments[kbd_id] = CandidateAssessment(
                kbd_id=kbd_id,
                state=CandidateState.NOT_EXECUTABLE,
                reasons=list(plan.compile_errors[kbd_id]),
            )
        else:
            assessments[kbd_id] = CandidateAssessment(kbd_id=kbd_id)
    return assessments


def reduce_candidates(
    plan: SignalPlan,
    assessments: dict[str, CandidateAssessment],
    *,
    finalize: bool = False,
    fusion_mode: str = FUSION_ROOT_OR_PROCESS,
) -> None:
    for kbd_id, assessment in assessments.items():
        if assessment.state in (CandidateState.NOT_EXECUTABLE, CandidateState.REJECTED):
            continue
        if assessment.scope_state == "UNKNOWN":
            # 即使所有现场信号都成立，适用范围未知也不能确认案例。
            assessment.state = CandidateState.INCONCLUSIVE
            continue
        refs = [ref for ref in plan.signals.values() if ref.kbd_id == kbd_id]
        track_refs = _group_by_track(refs)
        if not track_refs:
            # 无可执行信号：保持原状（单路时代由 `must_refs 为空` 分支兜底）。
            continue
        policy = plan.verification_policies.get(kbd_id)
        assessment.track_states = {}
        assessment.track_reasons = {}
        assessment.track_rejected_by = {}
        for track, refs_in_track in track_refs.items():
            state, reasons, rejected_by = _reduce_track(refs_in_track, assessment, policy, finalize)
            assessment.track_states[track] = state
            assessment.track_reasons[track] = reasons
            if rejected_by:
                assessment.track_rejected_by[track] = rejected_by
        assessment.state = fuse_track_states(
            assessment.track_states,
            rejected_by=assessment.track_rejected_by,
            mode=fusion_mode,
        )
        assessment.reasons = _merged_reasons(track_refs, assessment.track_reasons)


def _group_by_track(refs: list) -> dict[str, list]:
    """按 track 分组；缺失 track 的历史数据归入 process，保持改造前语义。"""
    grouped: dict[str, list] = {}
    for ref in refs:
        grouped.setdefault(getattr(ref, "track", PROCESS_TRACK) or PROCESS_TRACK, []).append(ref)
    return grouped


def _reduce_track(
    refs: list, assessment: CandidateAssessment, policy, finalize: bool
) -> tuple[CandidateState, list[str], str | None]:
    """单路归约：返回值第三项为拒绝原因（must/exclude），供跨路 EXCLUDE 否决使用。"""
    def outcome(ref) -> SignalOutcome:
        return assessment.signal_outcomes.get(ref.ref_id, SignalOutcome.NOT_RUN)

    must_refs = [ref for ref in refs if ref.evidence_role is EvidenceRole.MUST]
    should_refs = [ref for ref in refs if ref.evidence_role is EvidenceRole.SHOULD]
    exclude_refs = [ref for ref in refs if ref.evidence_role is EvidenceRole.EXCLUDE]
    must_outcomes = [outcome(must_ref) for must_ref in must_refs]
    contradicted_must = [ref for ref in must_refs if outcome(ref) is SignalOutcome.CONTRADICTED]
    satisfied_excludes = [ref for ref in exclude_refs if outcome(ref) is SignalOutcome.SATISFIED]
    if contradicted_must or satisfied_excludes:
        reasons = (
            [f"must signal CONTRADICTED: {ref.signal_id}" for ref in contradicted_must]
            + [f"exclude signal SATISFIED: {ref.signal_id}" for ref in satisfied_excludes]
        )
        return CandidateState.REJECTED, reasons, ("exclude" if satisfied_excludes else "must")

    minimum_should = policy.minimum_should if policy is not None else 0
    satisfied_should = sum(1 for ref in should_refs if outcome(ref) is SignalOutcome.SATISFIED)
    excludes_cleared = all(outcome(ref) is SignalOutcome.CONTRADICTED for ref in exclude_refs)
    if (
        must_refs
        and all(item is SignalOutcome.SATISFIED for item in must_outcomes)
        and satisfied_should >= minimum_should
        and excludes_cleared
    ):
        return CandidateState.SUPPORTED, [
            "all must signals SATISFIED, exclude signals cleared, and minimum_should reached"
        ], None
    if finalize:
        unresolved = [
            f"{ref.signal_id}={outcome(ref).value}"
            for ref in must_refs + exclude_refs
            if outcome(ref)
            not in (
                SignalOutcome.SATISFIED if ref.evidence_role is EvidenceRole.MUST else SignalOutcome.CONTRADICTED,
            )
        ]
        if satisfied_should < minimum_should:
            unresolved.append(f"should={satisfied_should}/{minimum_should}")
        return CandidateState.INCONCLUSIVE, unresolved or ["required evidence incomplete"], None
    return CandidateState.CANDIDATE, [], None


def _merged_reasons(track_refs: dict[str, list], track_reasons: dict[str, list[str]]) -> list[str]:
    """多路时给原因加来源前缀；单路保持原文，避免污染既有报告文案与断言。"""
    if len(track_refs) <= 1:
        for reasons in track_reasons.values():
            return list(reasons)
        return []
    merged: list[str] = []
    for track, reasons in track_reasons.items():
        merged.extend(f"[{track}] {reason}" for reason in reasons)
    return merged
