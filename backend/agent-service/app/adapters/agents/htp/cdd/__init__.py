"""Deterministic KBD candidate diagnosis core."""

from .conclusion_gate import decide_conclusion
from .coverage import CandidateCoverage, CoverageReport, build_coverage_report
from .models import (
    Acquisition,
    CandidateAssessment,
    CandidateState,
    CaseVerdict,
    CaseVerificationPolicy,
    ConclusionDecision,
    ConclusionLevel,
    EvidenceRole,
    SignalEvaluation,
    SignalOutcome,
    SignalPlan,
    SignalRef,
)
from .plan_compiler import compile_signal_plan
from .replay import ReplayError, replay_evaluations
from .scheduler import ActiveDiagnosticScheduler, SchedulerWeights
from .scope import ScopeResult, ScopeState, apply_scope_results, evaluate_scope

__all__ = [
    "Acquisition",
    "ActiveDiagnosticScheduler",
    "CandidateAssessment",
    "CandidateCoverage",
    "CandidateState",
    "CaseVerdict",
    "CaseVerificationPolicy",
    "ConclusionDecision",
    "ConclusionLevel",
    "CoverageReport",
    "EvidenceRole",
    "ReplayError",
    "SchedulerWeights",
    "ScopeResult",
    "ScopeState",
    "SignalEvaluation",
    "SignalOutcome",
    "SignalPlan",
    "SignalRef",
    "build_coverage_report",
    "apply_scope_results",
    "compile_signal_plan",
    "decide_conclusion",
    "evaluate_scope",
    "replay_evaluations",
]
