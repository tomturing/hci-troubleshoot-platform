"""Shared Resolution Runtime 注册与生命周期。"""

from __future__ import annotations

from typing import Any

from shared.resolution.models import ResolutionPlan, ResolvedAcquisition, SignalIntent
from shared.resolution.resolvers import (
    DomainResolver,
    LogResolver,
    QkvResolver,
    Resolver,
    ServiceResolver,
    SystemResolver,
    VariableResolver,
)


class SharedResolutionRuntime:
    """一个平台、多个领域 Resolver；Registry 是唯一路由入口。"""

    def __init__(self, resolvers: list[Resolver] | None = None) -> None:
        self._resolvers = {item.resolver_id: item for item in (resolvers or [LogResolver(), SystemResolver(), DomainResolver(), ServiceResolver(), QkvResolver(), VariableResolver()])}

    def get(self, resolver_id: str) -> Resolver:
        try:
            return self._resolvers[resolver_id]
        except KeyError as exc:
            raise ValueError(f"未知 resolver_id: {resolver_id}") from exc

    def compile(self, intent: SignalIntent) -> ResolutionPlan:
        return self.get(intent.resolver_id).compile(intent)

    def resolve(self, plan: ResolutionPlan, context: dict[str, Any] | None = None) -> ResolvedAcquisition:
        return self.get(plan.resolver_id).resolve(plan, context)

    def compile_and_resolve(self, intent: SignalIntent, context: dict[str, Any] | None = None) -> tuple[ResolutionPlan, ResolvedAcquisition]:
        plan = self.compile(intent)
        return plan, self.resolve(plan, context)


_RUNTIME = SharedResolutionRuntime()


def get_resolution_runtime() -> SharedResolutionRuntime:
    return _RUNTIME

