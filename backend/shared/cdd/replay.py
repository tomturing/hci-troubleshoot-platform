"""对不可变 SignalEvaluation（信号评估）记录执行确定性回放。"""

from __future__ import annotations

from .candidate_reducer import initial_assessments, reduce_candidates
from .coverage import CoverageReport, build_coverage_report
from .models import CandidateAssessment, SignalEvaluation, SignalOutcome, SignalPlan
from .scope import apply_scope_results


class ReplayError(ValueError):
    """评估日志不完整、互相冲突或引用了其他计划。"""


def replay_evaluations(
    plan: SignalPlan,
    evaluations: list[SignalEvaluation],
    environment: dict[str, object],
) -> tuple[dict[str, CandidateAssessment], CoverageReport]:
    assessments = initial_assessments(plan)
    apply_scope_results(plan, assessments, environment)
    seen: set[str] = set()
    for evaluation in evaluations:
        if evaluation.signal_ref_id not in plan.signals:
            raise ReplayError(f"unknown signal_ref_id: {evaluation.signal_ref_id}")
        if evaluation.signal_ref_id in seen:
            raise ReplayError(f"duplicate evaluation for signal_ref_id: {evaluation.signal_ref_id}")
        if not evaluation.evaluation_id or not evaluation.exec_id:
            raise ReplayError("evaluation_id and exec_id are required for auditable replay")
        outcome = SignalOutcome(evaluation.outcome)
        if outcome in {SignalOutcome.NOT_RUN, SignalOutcome.BLOCKED}:
            raise ReplayError(f"internal scheduler state cannot be replayed: {outcome.value}")
        ref = plan.signals[evaluation.signal_ref_id]
        assessments[ref.kbd_id].signal_outcomes[ref.ref_id] = outcome
        seen.add(ref.ref_id)
    reduce_candidates(plan, assessments, finalize=True)
    return assessments, build_coverage_report(plan, assessments)
