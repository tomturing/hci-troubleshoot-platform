"""Deterministic active acquisition scheduler.

The heuristic changes execution order only. Candidate truth is owned by the reducer
and conclusion gate.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import Acquisition, CandidateAssessment, CandidateState, SignalPlan


@dataclass(frozen=True)
class SchedulerWeights:
    discrimination: float = 4.0
    required_coverage: float = 2.0
    unlock: float = 3.0
    reuse: float = 1.0
    cost: float = 0.25
    latency: float = 0.15
    risk: float = 1.0


@dataclass(frozen=True)
class AcquisitionScore:
    utility: float
    discrimination: float
    required_coverage: float
    unlock: float
    reuse: float
    cost: float
    latency: float
    risk: float


class ActiveDiagnosticScheduler:
    def __init__(self, plan: SignalPlan, weights: SchedulerWeights | None = None) -> None:
        self.plan = plan
        self.weights = weights or SchedulerWeights()
        self.completed: set[str] = set()

    @staticmethod
    def _active(assessment: CandidateAssessment) -> bool:
        return assessment.state is CandidateState.CANDIDATE

    def _linked_active_refs(
        self,
        acquisition: Acquisition,
        assessments: dict[str, CandidateAssessment],
    ):
        return [ref for ref in acquisition.signal_refs if self._active(assessments[ref.kbd_id])]

    def executable(
        self,
        assessments: dict[str, CandidateAssessment],
        available_variables: set[str],
    ) -> list[Acquisition]:
        available = {name.lower() for name in available_variables}
        result: list[Acquisition] = []
        for key, acquisition in self.plan.acquisitions.items():
            if key in self.completed or not self._linked_active_refs(acquisition, assessments):
                continue
            if acquisition.requires.issubset(available):
                result.append(acquisition)
        return result

    def score(
        self,
        acquisition: Acquisition,
        assessments: dict[str, CandidateAssessment],
    ) -> AcquisitionScore:
        refs = self._linked_active_refs(acquisition, assessments)
        required_refs = [ref for ref in refs if ref.required_for_support]
        matcher_groups = {ref.matcher_fingerprint for ref in refs}
        discrimination = float(len(refs) * max(0, len(matcher_groups) - 1))
        required_coverage = float(len(required_refs))
        reuse = float(max(0, len(refs) - 1))
        active_ids = {ref.kbd_id for ref in self.plan.signals.values() if self._active(assessments[ref.kbd_id])}
        unlock = 0.0
        for other in self.plan.acquisitions.values():
            if other.template_key in self.completed or not (other.requires & acquisition.produces):
                continue
            if any(ref.kbd_id in active_ids for ref in other.signal_refs):
                unlock += 1.0
        w = self.weights
        utility = (
            w.discrimination * discrimination
            + w.required_coverage * required_coverage
            + w.unlock * unlock
            + w.reuse * reuse
            - w.cost * acquisition.cost
            - w.latency * acquisition.latency
            - w.risk * acquisition.risk
        )
        return AcquisitionScore(
            utility=utility,
            discrimination=discrimination,
            required_coverage=required_coverage,
            unlock=unlock,
            reuse=reuse,
            cost=acquisition.cost,
            latency=acquisition.latency,
            risk=acquisition.risk,
        )

    def choose(
        self,
        assessments: dict[str, CandidateAssessment],
        available_variables: set[str],
    ) -> tuple[Acquisition, AcquisitionScore] | None:
        ranked = [(item, self.score(item, assessments)) for item in self.executable(assessments, available_variables)]
        if not ranked:
            return None
        ranked.sort(
            key=lambda pair: (
                -pair[1].utility,
                pair[0].risk,
                pair[0].cost,
                pair[0].latency,
                pair[0].template_key,
            )
        )
        return ranked[0]

    def mark_completed(self, acquisition: Acquisition) -> None:
        self.completed.add(acquisition.template_key)

    def remaining_signal_refs(self, assessments: dict[str, CandidateAssessment]):
        for acquisition in self.plan.acquisitions.values():
            if acquisition.template_key in self.completed:
                continue
            yield from self._linked_active_refs(acquisition, assessments)
