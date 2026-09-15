"""在线与离线诊断共用的采集提供器边界和确定性计划运行器。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from .models import Acquisition, CandidateAssessment, SignalOutcome, SignalPlan
from .scheduler import ActiveDiagnosticScheduler


@dataclass(frozen=True)
class ProducedVariable:
    """生产者实际提取出的变量及其可审计来源。

    ``produced_variables`` 只回答调度器“某个名字是否已就绪”。消费者真正需要的是
    已提取的值，而且离线回放还必须能指出该值来自哪条证据。两者分开保留，避免把
    仅用于旧 Provider 兼容的变量名集合误当成可安全下发的参数值。
    """

    value: Any
    evidence_refs: tuple[str, ...] = ()
    source_signal_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class AcquisitionRunResult:
    """一次采集对多个 SignalRef 的标准化结果。"""

    outcomes: dict[str, SignalOutcome]
    produced_variables: frozenset[str] = frozenset()
    produced_values: Mapping[str, ProducedVariable] = field(default_factory=dict)
    evaluations: tuple[dict[str, Any], ...] = field(default_factory=tuple)


class AcquisitionProvider(Protocol):
    """采集模式适配器；实现方不得改变 CDD 的结果语义。"""

    async def acquire(
        self,
        acquisition: Acquisition,
        *,
        variable_values: Mapping[str, ProducedVariable],
    ) -> AcquisitionRunResult:
        """执行或查询一次采集，并按 SignalRef 返回认识论结果。"""


async def execute_acquisition_plan(
    plan: SignalPlan,
    assessments: dict[str, CandidateAssessment],
    provider: AcquisitionProvider,
    *,
    available_variables: set[str] | None = None,
    available_variable_values: Mapping[str, ProducedVariable] | None = None,
) -> tuple[set[str], list[dict[str, Any]]]:
    """按同一依赖调度语义执行在线或离线 Provider。"""

    variables = {str(name).lower() for name in (available_variables or set())}
    variable_values = {
        str(name).lower(): value
        for name, value in (available_variable_values or {}).items()
        if isinstance(value, ProducedVariable)
    }
    variables.update(variable_values)
    conflicted_variables: set[str] = set()
    evaluations: list[dict[str, Any]] = []
    scheduler = ActiveDiagnosticScheduler(plan)
    while selected := scheduler.choose(assessments, variables):
        acquisition, _score = selected
        result = await provider.acquire(acquisition, variable_values=dict(variable_values))
        for ref in acquisition.signal_refs:
            assessments[ref.kbd_id].signal_outcomes[ref.ref_id] = result.outcomes.get(
                ref.ref_id,
                SignalOutcome.UNKNOWN,
            )
        produced_names = {str(name).lower() for name in result.produced_variables}
        produced_names.update(str(name).lower() for name in result.produced_values)
        conflicts: list[str] = []
        for name, value in result.produced_values.items():
            normalized = str(name).lower()
            if not isinstance(value, ProducedVariable):
                continue
            if normalized in conflicted_variables:
                conflicts.append(normalized)
                continue
            previous = variable_values.get(normalized)
            if previous is not None and previous.value != value.value:
                # 同名变量出现不同值时不能选择“后来的值”。从可用集合撤销该名字，
                # 让依赖它的采集保持 BLOCKED，并在本次信号审计中显式记录冲突。
                variable_values.pop(normalized, None)
                variables.discard(normalized)
                conflicted_variables.add(normalized)
                conflicts.append(normalized)
                continue
            if normalized not in conflicts:
                variable_values[normalized] = value
        variables.update(produced_names - conflicted_variables)
        evaluations.extend(
            {
                **item,
                **({"variable_pool_conflicts": sorted(conflicts)} if conflicts else {}),
            }
            for item in result.evaluations
        )
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
