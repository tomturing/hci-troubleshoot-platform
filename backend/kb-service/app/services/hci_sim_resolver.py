"""hci-sim 阶段 C 的权威 KBD 解析器。

解析器只读取已发布 KBD 对应的 ``dynamic_resource_active`` 不可变快照；它绝不调用
``ensure_published``，避免批量验证把编辑态 KBD 意外推进到 Agent active。缺少必要事实时
返回结构化 capability gap，而不是补猜 revision、Tool Contract 或 Artifact。
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from shared.models.dynamic_resource import DynamicResourceActive, DynamicResourceRevision
from shared.resolution.models import ResolutionStatus
from shared.resolution.review import SignalReviewFeature, review_signal_document
from shared.schemas.hci_sim_policy import current_hci_sim_policy_revision
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.kbd_entry import KbdEntry


def _sha256(value: Any) -> str:
    """生成跨进程稳定的 JSON 指纹。"""

    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()


def _digest(value: str) -> str:
    """统一外部 digest 表示，拒绝空值而不是制造伪 checksum。"""

    normalized = value.strip()
    if not normalized:
        return ""
    return normalized if normalized.startswith("sha256:") else "sha256:" + normalized


@dataclass(frozen=True)
class CapabilityGap:
    """不给调用方泄露原始 Artifact 或内部 SQL 的可操作能力缺口。"""

    code: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True)
class ResolvedKbdInput:
    """可传给 Fixture Compiler 的不可变 KBD/Contract 输入，不包含 Artifact。"""

    support_id: str
    kbd_id: int
    kbd_revision: int
    kbd_checksum: str
    signals_digest: str
    tool_contract_revision: str
    policy_revision: str
    source_trace_id: str | None
    metadata: dict[str, Any]
    verification_contract: dict[str, Any]
    synthetic_routes: tuple[SyntheticRouteInput, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "support_id": self.support_id,
            "kbd_id": self.kbd_id,
            "kbd_revision": self.kbd_revision,
            "kbd_checksum": self.kbd_checksum,
            "signals_digest": self.signals_digest,
            "tool_contract_revision": self.tool_contract_revision,
            "policy_revision": self.policy_revision,
            "source_trace_id": self.source_trace_id,
            "metadata": self.metadata,
            "verification_contract": self.verification_contract,
            "synthetic_routes": [route.to_dict() for route in self.synthetic_routes],
        }


@dataclass(frozen=True)
class SyntheticRouteInput:
    """由已发布 Signal 和 Tool 修订确定的最小模拟路由输入。"""

    signal_id: str
    tool: str
    argv: tuple[str, ...]
    tool_revision: int
    tool_checksum: str
    required_variables: tuple[str, ...] = ()
    role: str = "context"
    matcher: dict[str, Any] | None = None
    produces: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "signal_id": self.signal_id,
            "tool": self.tool,
            "argv": list(self.argv),
            "tool_revision": self.tool_revision,
            "tool_checksum": self.tool_checksum,
            "required_variables": list(self.required_variables),
            "role": self.role,
            "matcher": self.matcher,
            "produces": list(self.produces),
        }


@dataclass(frozen=True)
class KbdResolution:
    """单条 KBD 的无副作用解析结果。"""

    support_id: str
    status: str
    metadata: dict[str, Any] = field(default_factory=dict)
    resolved: ResolvedKbdInput | None = None
    gaps: tuple[CapabilityGap, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "support_id": self.support_id,
            "status": self.status,
            "metadata": self.metadata,
            "resolved": self.resolved.to_dict() if self.resolved is not None else None,
            "capability_gaps": [gap.to_dict() for gap in self.gaps],
        }


@dataclass(frozen=True)
class KbdResolutionReport:
    """批量可编译性报告；ready 仍只表示下一步可绑定 Artifact。"""

    results: tuple[KbdResolution, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        status_counts = Counter(item.status for item in self.results)
        gap_counts = Counter(gap.code for item in self.results for gap in item.gaps)
        return {
            "total": len(self.results),
            "status_counts": dict(sorted(status_counts.items())),
            "gap_counts": dict(sorted(gap_counts.items())),
            "results": [item.to_dict() for item in self.results],
            "facts_boundary": (
                "ready_for_artifact_binding 仅表示 KBD/Signal/Contract/Policy 快照已冻结；"
                "未绑定获批 Artifact 时不得编译 positive-realistic Bundle 或宣称 E2E 已验证。"
            ),
        }


class HciSimKbdResolver:
    """解析已发布 KBD 的 active 快照，并批量输出稳定 capability gaps。"""

    async def resolve_support_id(self, session: AsyncSession, support_id: str) -> KbdResolution:
        result = await session.execute(select(KbdEntry).where(KbdEntry.support_id == support_id))
        entry = result.scalar_one_or_none()
        if entry is None:
            return KbdResolution(
                support_id=support_id,
                status="capability_gap",
                gaps=(CapabilityGap("KBD_NOT_FOUND", "support_id 不存在"),),
            )
        snapshots = await self._active_snapshots(session)
        tool_snapshots = await self._active_tool_snapshots(session)
        return self.resolve_entry(entry, snapshots.get(str(entry.id)), tool_snapshots)

    async def resolve_all(self, session: AsyncSession) -> KbdResolutionReport:
        entries_result = await session.execute(select(KbdEntry).order_by(KbdEntry.support_id))
        snapshots = await self._active_snapshots(session)
        tool_snapshots = await self._active_tool_snapshots(session)
        results = tuple(
            self.resolve_entry(entry, snapshots.get(str(entry.id)), tool_snapshots)
            for entry in entries_result.scalars()
        )
        return KbdResolutionReport(results)

    async def _active_snapshots(
        self, session: AsyncSession
    ) -> dict[str, tuple[DynamicResourceActive, DynamicResourceRevision]]:
        rows = await session.execute(
            select(DynamicResourceActive, DynamicResourceRevision)
            .join(
                DynamicResourceRevision,
                and_(
                    DynamicResourceRevision.resource_type == DynamicResourceActive.resource_type,
                    DynamicResourceRevision.resource_name == DynamicResourceActive.resource_name,
                    DynamicResourceRevision.revision == DynamicResourceActive.active_revision,
                ),
            )
            .where(DynamicResourceActive.resource_type == "kbd")
        )
        return {str(active.resource_name): (active, revision) for active, revision in rows.all()}

    async def _active_tool_snapshots(
        self, session: AsyncSession
    ) -> dict[str, tuple[DynamicResourceActive, DynamicResourceRevision]]:
        """读取 Tool Registry（工具注册表）当前不可变修订。"""

        rows = await session.execute(
            select(DynamicResourceActive, DynamicResourceRevision)
            .join(
                DynamicResourceRevision,
                and_(
                    DynamicResourceRevision.resource_type == DynamicResourceActive.resource_type,
                    DynamicResourceRevision.resource_name == DynamicResourceActive.resource_name,
                    DynamicResourceRevision.revision == DynamicResourceActive.active_revision,
                ),
            )
            .where(DynamicResourceActive.resource_type == "tool")
        )
        return {str(active.resource_name): (active, revision) for active, revision in rows.all()}

    def resolve_entry(
        self,
        entry: KbdEntry | Any,
        active_snapshot: tuple[DynamicResourceActive, DynamicResourceRevision] | None,
        tool_snapshots: dict[str, tuple[DynamicResourceActive, DynamicResourceRevision]] | None = None,
    ) -> KbdResolution:
        """解析 ORM 行或测试替身；只要有一个强制事实缺失就 fail closed。"""

        support_id = str(getattr(entry, "support_id", "") or "")
        entry_metadata = (
            dict(getattr(entry, "entry_metadata", {}) or {})
            if isinstance(getattr(entry, "entry_metadata", {}), dict)
            else {}
        )
        gaps: list[CapabilityGap] = []
        if getattr(entry, "status", None) != "published":
            gaps.append(CapabilityGap("KBD_NOT_PUBLISHED", "KBD 未处于 published 状态"))
        if active_snapshot is None:
            gaps.append(CapabilityGap("KBD_ACTIVE_SNAPSHOT_MISSING", "不存在对应的 active 不可变 KBD 快照"))
            return KbdResolution(
                support_id=support_id, status="capability_gap", metadata=entry_metadata, gaps=tuple(gaps)
            )

        active, snapshot = active_snapshot
        if snapshot.status != "published":
            gaps.append(CapabilityGap("KBD_ACTIVE_SNAPSHOT_NOT_PUBLISHED", "active KBD 快照不是 published"))
        if _digest(active.checksum) != _digest(snapshot.checksum):
            gaps.append(CapabilityGap("KBD_ACTIVE_CHECKSUM_MISMATCH", "active 指针与快照 checksum 不一致"))

        content = snapshot.content_json if isinstance(snapshot.content_json, dict) else {}
        if str(content.get("support_id") or "") != support_id:
            gaps.append(CapabilityGap("KBD_SNAPSHOT_IDENTITY_MISMATCH", "active 快照不属于该 support_id"))
        signals_document = content.get("signals_json")
        if not isinstance(signals_document, dict):
            gaps.append(CapabilityGap("SIGNALS_DOCUMENT_INVALID", "active 快照没有 v2 Signal 文档"))
            return KbdResolution(
                support_id=support_id, status="capability_gap", metadata=entry_metadata, gaps=tuple(gaps)
            )
        signals = signals_document.get("signals")
        if not isinstance(signals, list) or not signals:
            gaps.append(CapabilityGap("SIGNALS_MISSING", "active KBD 快照没有可编译 Signal"))

        publish_validation = signals_document.get("publish_validation")
        if not isinstance(publish_validation, dict) or publish_validation.get("status") != "passed":
            gaps.append(CapabilityGap("SIGNALS_NOT_PUBLISH_VALIDATED", "Signal 缺少通过的 publish_validation"))
        tool_revision = str((publish_validation or {}).get("tool_contract_revision") or "")
        if not tool_revision:
            gaps.append(CapabilityGap("TOOL_CONTRACT_REVISION_MISSING", "Signal 发布记录缺少 Tool Contract revision"))
        # 对齐 #755 校验器驱动门禁：tool_contract_revision 字节哈希仅作粗粒度变化探测，
        # 不再据此阻断 sim 绑定。旧信号能否在新契约下执行由 playbooks 路由的顶层
        # validate_publishable_signals_json 判定（agent 选 KBD 前已过滤 executable=False）。
        # 此处保留 tool_revision 供 ResolvedKbdInput 追溯，staleness 仅观测不阻断。

        synthetic_routes: tuple[SyntheticRouteInput, ...] = ()
        if isinstance(signals, list) and signals:
            synthetic_routes, route_gaps = self._resolve_synthetic_routes(signals_document, tool_snapshots or {})
            gaps.extend(route_gaps)

        snapshot_metadata = dict(
            (
                getattr(snapshot, "contract_json", {})
                if isinstance(getattr(snapshot, "contract_json", {}), dict)
                else {}
            ).get("metadata")
            or entry_metadata
        )
        if gaps:
            return KbdResolution(
                support_id=support_id, status="capability_gap", metadata=snapshot_metadata, gaps=tuple(gaps)
            )
        resolved = ResolvedKbdInput(
            support_id=support_id,
            kbd_id=int(entry.id),
            kbd_revision=int(snapshot.revision),
            kbd_checksum=_digest(snapshot.checksum),
            signals_digest=_sha256(signals_document),
            tool_contract_revision=tool_revision,
            policy_revision=current_hci_sim_policy_revision(),
            source_trace_id=snapshot.trace_id,
            metadata=snapshot_metadata,
            verification_contract=dict(signals_document.get("verification_contract") or {}),
            synthetic_routes=synthetic_routes,
        )
        return KbdResolution(
            support_id=support_id,
            status="ready_for_artifact_binding",
            metadata=snapshot_metadata,
            resolved=resolved,
        )

    @staticmethod
    def _resolve_synthetic_routes(
        signals_document: dict[str, Any],
        tool_snapshots: dict[str, tuple[DynamicResourceActive, DynamicResourceRevision]],
    ) -> tuple[tuple[SyntheticRouteInput, ...], list[CapabilityGap]]:
        """从当前 Signal Runtime 与 Tool Registry 生成路由，不识别具体 KBD。"""

        review = review_signal_document(signals_document, feature=SignalReviewFeature.PUBLISH)
        gaps: list[CapabilityGap] = []
        routes: list[SyntheticRouteInput] = []
        unresolved_signal_ids: list[str] = []
        signals = signals_document.get("signals") or []
        reviews = {item.signal_index: item for item in review.signals}
        for index, signal in enumerate(signals):
            if not isinstance(signal, dict):
                continue
            signal_id = str(signal.get("id") or f"signals[{index}]")
            acquire = signal.get("acquire") if isinstance(signal.get("acquire"), dict) else {}
            orchestrate = signal.get("orchestrate") if isinstance(signal.get("orchestrate"), dict) else {}
            tool = str(acquire.get("tool") or "").strip()
            tool_snapshot = tool_snapshots.get(tool)
            if tool_snapshot is None:
                gaps.append(CapabilityGap("TOOL_ACTIVE_SNAPSHOT_MISSING", f"Tool {tool} 没有 active 不可变修订"))
                continue
            active, revision = tool_snapshot
            content = revision.content_json if isinstance(revision.content_json, dict) else {}
            if (
                revision.status != "published"
                or not bool(content.get("is_active"))
                or _digest(active.checksum) != _digest(revision.checksum)
            ):
                gaps.append(
                    CapabilityGap("TOOL_ACTIVE_SNAPSHOT_INVALID", f"Tool {tool} 当前修订未发布、已停用或校验不一致")
                )
                continue
            runtime = reviews.get(index)
            if runtime is None or runtime.status is ResolutionStatus.BLOCKED or not runtime.command:
                unresolved_signal_ids.append(signal_id)
                continue
            try:
                argv = tuple(shlex.split(runtime.command))
            except ValueError as exc:
                unresolved_signal_ids.append(f"{signal_id}（命令无法分词：{exc}）")
                continue
            if not argv:
                unresolved_signal_ids.append(signal_id)
                continue
            required_variables = tuple(
                sorted(
                    {
                        variable
                        for item in argv
                        for variable in re.findall(r"\{\{([A-Z][A-Z0-9_]*)\}\}", item)
                    }
                )
            )
            routes.append(
                SyntheticRouteInput(
                    signal_id=signal_id,
                    tool=tool,
                    argv=argv,
                    tool_revision=int(revision.revision),
                    tool_checksum=_digest(revision.checksum),
                    required_variables=required_variables,
                    role=str(signal.get("role") or "context"),
                    matcher=dict(signal.get("match") or {}) or None,
                    produces=tuple(
                        dict(item) for item in (orchestrate.get("produces") or []) if isinstance(item, dict)
                    ),
                )
            )
        if not routes and not gaps:
            detail = "、".join(unresolved_signal_ids[:5])
            gaps.append(
                CapabilityGap(
                    "SYNTHETIC_ROUTE_UNRESOLVED",
                    f"当前没有可确定编译的 Signal 模拟路由{f'：{detail}' if detail else ''}",
                )
            )
        return tuple(routes), gaps
