"""Evidence coverage report derived only from compiled plan and replayable outcomes."""

from __future__ import annotations

from dataclasses import dataclass

from .models import CandidateAssessment, EvidenceRole, SignalOutcome, SignalPlan


@dataclass(frozen=True)
class CandidateCoverage:
    kbd_id: str
    executable: bool
    total: int
    observed: int
    must_total: int
    must_satisfied: int
    should_total: int
    should_satisfied: int
    minimum_should: int
    exclude_total: int
    exclude_cleared: int
    unresolved_signal_ids: tuple[str, ...]

    @property
    def observation_ratio(self) -> float:
        return self.observed / self.total if self.total else 0.0


@dataclass(frozen=True)
class CoverageReport:
    schema_version: int
    plan_id: str
    candidates: tuple[CandidateCoverage, ...]


def build_coverage_report(
    plan: SignalPlan,
    assessments: dict[str, CandidateAssessment],
) -> CoverageReport:
    rows: list[CandidateCoverage] = []
    for kbd_id in sorted(plan.candidates):
        assessment = assessments[kbd_id]
        refs = [ref for ref in plan.signals.values() if ref.kbd_id == kbd_id]

        def outcome(ref, current=assessment):
            return current.signal_outcomes.get(ref.ref_id, SignalOutcome.NOT_RUN)

        must = [ref for ref in refs if ref.evidence_role is EvidenceRole.MUST]
        should = [ref for ref in refs if ref.evidence_role is EvidenceRole.SHOULD]
        exclude = [ref for ref in refs if ref.evidence_role is EvidenceRole.EXCLUDE]
        observed = [
            ref for ref in refs
            if outcome(ref) not in {SignalOutcome.NOT_RUN, SignalOutcome.BLOCKED}
        ]
        unresolved = [
            ref.signal_id for ref in refs
            if outcome(ref) in {
                SignalOutcome.NOT_RUN,
                SignalOutcome.BLOCKED,
                SignalOutcome.UNKNOWN,
                SignalOutcome.ERROR,
                SignalOutcome.NOT_APPLICABLE,
            }
        ]
        policy = plan.verification_policies.get(kbd_id)
        rows.append(CandidateCoverage(
            kbd_id=kbd_id,
            executable=kbd_id not in plan.compile_errors,
            total=len(refs),
            observed=len(observed),
            must_total=len(must),
            must_satisfied=sum(outcome(ref) is SignalOutcome.SATISFIED for ref in must),
            should_total=len(should),
            should_satisfied=sum(outcome(ref) is SignalOutcome.SATISFIED for ref in should),
            minimum_should=policy.minimum_should if policy else 0,
            exclude_total=len(exclude),
            exclude_cleared=sum(outcome(ref) is SignalOutcome.CONTRADICTED for ref in exclude),
            unresolved_signal_ids=tuple(sorted(unresolved)),
        ))
    return CoverageReport(schema_version=1, plan_id=plan.plan_id, candidates=tuple(rows))
