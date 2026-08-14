"""结论门禁：唯一允许授权进入 S4 阶段的组件。"""

from __future__ import annotations

from .models import CandidateAssessment, CandidateState, ConclusionDecision, ConclusionLevel


def decide_conclusion(assessments: dict[str, CandidateAssessment]) -> ConclusionDecision:
    by_state = {
        state: tuple(sorted(item.kbd_id for item in assessments.values() if item.state is state))
        for state in CandidateState
    }
    supported = by_state[CandidateState.SUPPORTED]
    rejected = by_state[CandidateState.REJECTED]
    inconclusive = by_state[CandidateState.INCONCLUSIVE] + by_state[CandidateState.CANDIDATE]
    not_executable = by_state[CandidateState.NOT_EXECUTABLE]
    unresolved = inconclusive + not_executable
    if len(supported) == 1 and not unresolved:
        level = ConclusionLevel.DEFINITIVE
        reason = "exactly one supported candidate exists and every other candidate is rejected"
    elif supported:
        level = ConclusionLevel.PARTIAL
        reason = "supported candidates are not unique or unresolved candidates could still be supported"
    elif unresolved:
        level = ConclusionLevel.INCONCLUSIVE
        reason = "no supported candidate and required evidence remains unresolved"
    else:
        level = ConclusionLevel.NO_MATCH
        reason = "all executable candidates were rejected by CONTRADICTED must evidence"
    return ConclusionDecision(
        level=level,
        supported_ids=supported,
        rejected_ids=rejected,
        inconclusive_ids=tuple(sorted(inconclusive)),
        not_executable_ids=not_executable,
        reason=reason,
    )
