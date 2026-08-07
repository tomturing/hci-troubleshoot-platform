"""Shared Resolution Runtime。

本包只负责把关键信号从候选意图编译成结构化、可审计的采集计划，并在消费前
按当前上下文解析为可执行 acquisition。真正的输出解释仍由 qfk/qkv Matcher/Parser
负责；真正的远端执行仍由现有 Executor 负责。
"""

from shared.resolution.archive import ArchiveInspection, ArchiveKind, inspect_archive
from shared.resolution.models import (
    ResolutionAuditSnapshot,
    ResolutionIssue,
    ResolutionPlan,
    ResolutionStatus,
    ResolvedAcquisition,
    SignalIntent,
    build_resolution_audit_snapshot,
)
from shared.resolution.probes import CapabilityProbe
from shared.resolution.runtime import SharedResolutionRuntime, get_resolution_runtime

__all__ = [
    "ResolutionIssue",
    "ResolutionAuditSnapshot",
    "ResolutionPlan",
    "ResolutionStatus",
    "ResolvedAcquisition",
    "SignalIntent",
    "build_resolution_audit_snapshot",
    "CapabilityProbe",
    "ArchiveInspection",
    "ArchiveKind",
    "inspect_archive",
    "SharedResolutionRuntime",
    "get_resolution_runtime",
]
