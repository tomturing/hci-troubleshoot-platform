"""Deterministically reduce signal evidence into KBD candidate states."""

from __future__ import annotations

from .models import CandidateAssessment, CandidateState, SignalOutcome, SignalPlan


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
        refs = [ref for ref in plan.signals.values() if ref.kbd_id == kbd_id and ref.required_for_support]
        outcomes = [assessment.signal_outcomes.get(ref.ref_id, SignalOutcome.NOT_RUN) for ref in refs]
        rejecting = [
            ref
            for ref in refs
            if ref.failure_effect == "reject"
            and assessment.signal_outcomes.get(ref.ref_id, SignalOutcome.NOT_RUN) is SignalOutcome.FAIL
        ]
        if rejecting:
            assessment.state = CandidateState.REJECTED
            assessment.reasons = [f"required signal FAIL: {ref.signal_id}" for ref in rejecting]
        elif refs and all(outcome is SignalOutcome.PASS for outcome in outcomes):
            assessment.state = CandidateState.SUPPORTED
            assessment.reasons = ["all required signals PASS"]
        elif finalize:
            assessment.state = CandidateState.INCONCLUSIVE
            unresolved = [
                f"{ref.signal_id}={assessment.signal_outcomes.get(ref.ref_id, SignalOutcome.NOT_RUN).value}"
                for ref in refs
                if assessment.signal_outcomes.get(ref.ref_id, SignalOutcome.NOT_RUN) is not SignalOutcome.PASS
            ]
            assessment.reasons = unresolved or ["required evidence incomplete"]
        else:
            assessment.state = CandidateState.CANDIDATE
