"""确定性 KBD 候选诊断内核。"""

from .acquisition_provider import AcquisitionProvider, AcquisitionRunResult, execute_acquisition_plan
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
from .replay_manifest import build_kbd_replay_manifest
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
    "build_kbd_replay_manifest",
    "apply_scope_results",
    "compile_signal_plan",
    "decide_conclusion",
    "AcquisitionProvider",
    "AcquisitionRunResult",
    "execute_acquisition_plan",
    "evaluate_scope",
    "replay_evaluations",
]
