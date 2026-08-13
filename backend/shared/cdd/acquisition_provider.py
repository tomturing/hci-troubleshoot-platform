"""在线与离线诊断共用的采集提供器边界和确定性计划运行器。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from .models import Acquisition, CandidateAssessment, SignalOutcome, SignalPlan
from .scheduler import ActiveDiagnosticScheduler


@dataclass(frozen=True)
class AcquisitionRunResult:
    """一次采集对多个 SignalRef 的标准化结果。"""

    outcomes: dict[str, SignalOutcome]
    produced_variables: frozenset[str] = frozenset()
    evaluations: tuple[dict[str, Any], ...] = field(default_factory=tuple)


class AcquisitionProvider(Protocol):
    """采集模式适配器；实现方不得改变 CDD 的结果语义。"""

    async def acquire(self, acquisition: Acquisition) -> AcquisitionRunResult:
        """执行或查询一次采集，并按 SignalRef 返回认识论结果。"""


async def execute_acquisition_plan(
    plan: SignalPlan,
    assessments: dict[str, CandidateAssessment],
    provider: AcquisitionProvider,
    *,
    available_variables: set[str] | None = None,
) -> tuple[set[str], list[dict[str, Any]]]:
    """按同一依赖调度语义执行在线或离线 Provider。"""

    variables = {str(name).lower() for name in (available_variables or set())}
    evaluations: list[dict[str, Any]] = []
    scheduler = ActiveDiagnosticScheduler(plan)
    while selected := scheduler.choose(assessments, variables):
        acquisition, _score = selected
        result = await provider.acquire(acquisition)
        for ref in acquisition.signal_refs:
            assessments[ref.kbd_id].signal_outcomes[ref.ref_id] = result.outcomes.get(
                ref.ref_id,
                SignalOutcome.UNKNOWN,
            )
        variables.update(name.lower() for name in result.produced_variables)
        evaluations.extend(result.evaluations)
        scheduler.mark_completed(acquisition)

    for ref in scheduler.remaining_signal_refs(assessments):
        assessments[ref.kbd_id].signal_outcomes[ref.ref_id] = SignalOutcome.BLOCKED
        evaluations.append(
            {
                "support_id": ref.support_id,
                "signal_id": f"kbd:{ref.support_id}:{ref.signal_id}",
                "state": "UNKNOWN",
                "reason": f"依赖变量未就绪：{', '.join(sorted(set(ref.requires) - variables))}",
                "required_for_conclusion": ref.required_for_support,
                "evidence_status": "missing",
                "evidence_refs": [],
                "matcher_snapshot": {"_blocked_requires": sorted(ref.requires)},
            }
        )
    return variables, evaluations
