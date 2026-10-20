"""将信号证据确定性归并为 KBD 候选状态。"""

from __future__ import annotations

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
) -> None:
    for kbd_id, assessment in assessments.items():
        if assessment.state in (CandidateState.NOT_EXECUTABLE, CandidateState.REJECTED):
            continue
        if assessment.scope_state == "UNKNOWN":
            # 即使所有现场信号都成立，适用范围未知也不能确认案例。
            assessment.state = CandidateState.INCONCLUSIVE
            continue
        refs = [ref for ref in plan.signals.values() if ref.kbd_id == kbd_id]
        must_refs = [ref for ref in refs if ref.evidence_role is EvidenceRole.MUST]
        should_refs = [ref for ref in refs if ref.evidence_role is EvidenceRole.SHOULD]
        exclude_refs = [ref for ref in refs if ref.evidence_role is EvidenceRole.EXCLUDE]
        must_outcomes = [assessment.signal_outcomes.get(ref.ref_id, SignalOutcome.NOT_RUN) for ref in must_refs]
        contradicted_must = [
            ref
            for ref in must_refs
            if assessment.signal_outcomes.get(ref.ref_id, SignalOutcome.NOT_RUN)
            is SignalOutcome.CONTRADICTED
        ]
        satisfied_excludes = [
            ref
            for ref in exclude_refs
            if assessment.signal_outcomes.get(ref.ref_id, SignalOutcome.NOT_RUN)
            is SignalOutcome.SATISFIED
        ]
        if contradicted_must or satisfied_excludes:
            assessment.state = CandidateState.REJECTED
            assessment.reasons = (
                [f"must signal CONTRADICTED: {ref.signal_id}" for ref in contradicted_must]
                + [f"exclude signal SATISFIED: {ref.signal_id}" for ref in satisfied_excludes]
            )
            continue

        minimum_should = plan.verification_policies.get(kbd_id).minimum_should if kbd_id in plan.verification_policies else 0
        satisfied_should = sum(
            assessment.signal_outcomes.get(ref.ref_id, SignalOutcome.NOT_RUN)
            is SignalOutcome.SATISFIED
            for ref in should_refs
        )
        excludes_cleared = all(
            assessment.signal_outcomes.get(ref.ref_id, SignalOutcome.NOT_RUN)
            is SignalOutcome.CONTRADICTED
            for ref in exclude_refs
        )
        if (
            must_refs
            and all(outcome is SignalOutcome.SATISFIED for outcome in must_outcomes)
            and satisfied_should >= minimum_should
            and excludes_cleared
        ):
            assessment.state = CandidateState.SUPPORTED
            assessment.reasons = [
                "all must signals SATISFIED, exclude signals cleared, and minimum_should reached"
            ]
        elif finalize:
            assessment.state = CandidateState.INCONCLUSIVE
            unresolved = [
                f"{ref.signal_id}={assessment.signal_outcomes.get(ref.ref_id, SignalOutcome.NOT_RUN).value}"
                for ref in must_refs + exclude_refs
                if assessment.signal_outcomes.get(ref.ref_id, SignalOutcome.NOT_RUN)
                not in (
                    SignalOutcome.SATISFIED if ref.evidence_role is EvidenceRole.MUST else SignalOutcome.CONTRADICTED,
                )
            ]
            if satisfied_should < minimum_should:
                unresolved.append(f"should={satisfied_should}/{minimum_should}")
            assessment.reasons = unresolved or ["required evidence incomplete"]
        else:
            assessment.state = CandidateState.CANDIDATE
