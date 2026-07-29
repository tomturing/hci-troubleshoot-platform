"""Deterministic Case Verification Contract scope evaluation."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .models import CandidateAssessment, CandidateState, SignalPlan


class ScopeState(StrEnum):
    APPLICABLE = "APPLICABLE"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class ScopeResult:
    state: ScopeState
    reasons: tuple[str, ...] = ()


def evaluate_scope(scope: dict[str, Any], environment: dict[str, Any]) -> ScopeResult:
    """对产品、版本、组件和拓扑做 fail-closed 适用性判断。"""

    unknown: list[str] = []
    mismatches: list[str] = []
    for contract_key, env_key in (("products", "product"), ("versions", "version")):
        allowed = {str(value) for value in scope.get(contract_key) or []}
        if not allowed:
            continue
        actual = environment.get(env_key)
        if actual is None or str(actual).strip() == "":
            unknown.append(f"missing environment.{env_key}")
        elif str(actual) not in allowed:
            mismatches.append(f"environment.{env_key}={actual} not in {sorted(allowed)}")

    for contract_key, env_key in (("components", "components"), ("topology_constraints", "topology")):
        required = {str(value) for value in scope.get(contract_key) or []}
        if not required:
            continue
        actual_raw = environment.get(env_key)
        if actual_raw is None:
            unknown.append(f"missing environment.{env_key}")
            continue
        actual = {str(value) for value in actual_raw} if isinstance(actual_raw, (list, set, tuple)) else {str(actual_raw)}
        if not required.issubset(actual):
            mismatches.append(f"environment.{env_key} misses {sorted(required - actual)}")

    if mismatches:
        return ScopeResult(ScopeState.NOT_APPLICABLE, tuple(mismatches))
    if unknown:
        return ScopeResult(ScopeState.UNKNOWN, tuple(unknown))
    return ScopeResult(ScopeState.APPLICABLE)


def apply_scope_results(
    plan: SignalPlan,
    assessments: dict[str, CandidateAssessment],
    environment: dict[str, Any],
) -> dict[str, ScopeResult]:
    results: dict[str, ScopeResult] = {}
    for kbd_id, policy in plan.verification_policies.items():
        result = evaluate_scope(policy.scope, environment)
        results[kbd_id] = result
        assessments[kbd_id].scope_state = result.state.value
        if result.state is ScopeState.NOT_APPLICABLE:
            assessments[kbd_id].state = CandidateState.REJECTED
            assessments[kbd_id].reasons = [f"scope NOT_APPLICABLE: {reason}" for reason in result.reasons]
        elif result.state is ScopeState.UNKNOWN:
            assessments[kbd_id].state = CandidateState.INCONCLUSIVE
            assessments[kbd_id].reasons.extend(f"scope UNKNOWN: {reason}" for reason in result.reasons)
    return results
