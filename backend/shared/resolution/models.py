"""Shared Resolution Runtime 的不可变数据契约。"""

from __future__ import annotations

import json
from enum import StrEnum
from hashlib import sha256
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ResolutionStatus(StrEnum):
    COMPILED = "compiled"
    VERIFIED = "verified"
    NEEDS_PROBE = "needs_probe"
    BLOCKED = "blocked"


class ResolutionIssue(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: str
    message: str
    field: str | None = None
    level: str = "error"
    suggested_fix: str | None = None


class SignalIntent(BaseModel):
    """抽取层和领域 Resolver 之间的唯一输入形态。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    resolver_id: str
    tool: str | None = None
    args: dict[str, Any] = Field(default_factory=dict)
    evidence: list[str] = Field(default_factory=list)
    aliases: list[str] = Field(default_factory=list)
    source: str | None = None


class ResolutionPlan(BaseModel):
    """compile() 的不可变输出；不包含现场探测结果。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    resolver_id: str
    tool: str | None = None
    canonical_args: dict[str, Any] = Field(default_factory=dict)
    argv_template: list[str] = Field(default_factory=list)
    candidates: list[dict[str, Any]] = Field(default_factory=list)
    catalog_version: str = "unknown"
    status: ResolutionStatus = ResolutionStatus.COMPILED
    issues: list[ResolutionIssue] = Field(default_factory=list)


class ResolvedAcquisition(BaseModel):
    """resolve() 的输出；Executor 只应接受 ``status=verified``。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    resolver_id: str
    tool: str | None = None
    status: ResolutionStatus
    argv: list[str] = Field(default_factory=list)
    command: str | None = None
    absolute_path: str | None = None
    candidates_tried: list[str] = Field(default_factory=list)
    resolution_rule: str | None = None
    catalog_version: str = "unknown"
    variables_used: dict[str, Any] = Field(default_factory=dict)
    issues: list[ResolutionIssue] = Field(default_factory=list)
    evidence: dict[str, Any] = Field(default_factory=dict)

    @property
    def verified(self) -> bool:
        return self.status is ResolutionStatus.VERIFIED


class ResolutionAuditSnapshot(BaseModel):
    """不可变、可重放的 resolution 快照；不包含凭据或原始大输出。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    snapshot_id: str
    resolver_id: str
    tool: str | None = None
    catalog_version: str
    status: ResolutionStatus
    resolution_rule: str | None = None
    candidates_tried: list[str] = Field(default_factory=list)
    absolute_path: str | None = None
    argv: list[str] = Field(default_factory=list)
    evidence: dict[str, Any] = Field(default_factory=dict)


def build_resolution_audit_snapshot(plan: ResolutionPlan, acquisition: ResolvedAcquisition) -> ResolutionAuditSnapshot:
    """构建稳定摘要，确保同一 plan/acquisition 可离线重放和去重。"""

    payload = {
        "plan": plan.model_dump(mode="json"),
        "acquisition": acquisition.model_dump(mode="json"),
    }
    digest = sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return ResolutionAuditSnapshot(
        snapshot_id=f"resolution:{digest}",
        resolver_id=acquisition.resolver_id,
        tool=acquisition.tool,
        catalog_version=acquisition.catalog_version,
        status=acquisition.status,
        resolution_rule=acquisition.resolution_rule,
        candidates_tried=acquisition.candidates_tried,
        absolute_path=acquisition.absolute_path,
        argv=acquisition.argv,
        evidence=acquisition.evidence,
    )
