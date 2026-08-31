"""KBD PackageSnapshot 与 VerificationAsset 的事务服务。

所有写操作都锁定同一 ``kbd_package`` 行，使用观察到的 snapshot digest 做 CAS。
这样并发保存只能有一个成功，重试不会覆盖工作头或重复产生资产。
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from shared.observability.logger import get_logger
from shared.observability.metrics import (
    KBD_PACKAGE_CAS_CONFLICTS_TOTAL,
    KBD_PACKAGE_SNAPSHOT_TOTAL,
    KBD_VERIFICATION_ASSET_ATTACH_TOTAL,
)
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.kbd_entry import KbdEntry
from app.models.version_governance import KbdPackage, PackageSnapshot, VerificationAsset, VerificationSet

logger = get_logger("kb-service-version-governance")


class SnapshotConflictError(Exception):
    """工作区已被其他请求推进。"""


class VerificationAssetDigestError(ValueError):
    """验证资产的声明摘要与服务端规范化内容不一致。"""


def _digest(value: Any) -> str:
    """对规范化 JSON 计算稳定 SHA-256 身份。"""

    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


_VERIFICATION_ASSET_FIELDS = (
    "signal_id",
    "processing_index",
    "dataset_id",
    "input_digest",
    "deterministic_input",
    "ai_input",
    "raw_response_hash",
    "output_json",
    "evidence_json",
    "downstream_result",
    "model",
    "prompt_revision",
    "contract_version",
    "run_id",
    "result_status",
)


def verification_asset_digest(
    support_id: str,
    asset: dict[str, Any],
    snapshot_fields: dict[str, str],
) -> str:
    """由服务端冻结上下文和验证结果共同计算不可变资产身份。"""

    return _digest(
        {
            "support_id": support_id,
            "snapshot": dict(sorted(snapshot_fields.items())),
            "asset": {field: asset.get(field) for field in _VERIFICATION_ASSET_FIELDS},
        }
    )


async def _lock_package(session: AsyncSession, support_id: str, trace_id: str) -> KbdPackage:
    result = await session.execute(select(KbdPackage).where(KbdPackage.support_id == support_id).with_for_update())
    package = result.scalar_one_or_none()
    if package is None:
        # 首次保存也必须在数据库层收敛并发创建，再重新加锁读取唯一工作区行。
        await session.execute(
            pg_insert(KbdPackage)
            .values(
                package_id=uuid4(),
                support_id=support_id,
                workspace_version=1,
                status="draft_editing",
                trace_id=trace_id,
            )
            .on_conflict_do_nothing(index_elements=[KbdPackage.support_id])
        )
        package = await session.scalar(
            select(KbdPackage).where(KbdPackage.support_id == support_id).with_for_update()
        )
        if package is None:
            raise RuntimeError("无法创建或锁定 KbdPackage")
        await session.flush()
    return package


def _check_observed(package: KbdPackage, observed: str | None) -> None:
    if package.working_snapshot_digest is None:
        if observed is not None:
            KBD_PACKAGE_CAS_CONFLICTS_TOTAL.inc()
            raise SnapshotConflictError(
                f"working snapshot changed: observed={observed!r}, current=None"
            )
        return
    if observed is None or observed != package.working_snapshot_digest:
        KBD_PACKAGE_CAS_CONFLICTS_TOTAL.inc()
        raise SnapshotConflictError(
            f"working snapshot changed: observed={observed!r}, current={package.working_snapshot_digest!r}"
        )


async def create_snapshot(
    session: AsyncSession,
    *,
    support_id: str,
    observed_snapshot_digest: str | None,
    knowledge_snapshot_digest: str,
    signal_spec_digest: str,
    simulation_spec_digest: str,
    verification_set_digest: str | None,
    prompt_revision: str,
    tool_contract_revision: str,
    policy_revision: str,
    compiler_revision: str,
    manifest: dict[str, Any],
    actor_id: str,
    trace_id: str,
) -> PackageSnapshot:
    """创建不可变 PackageSnapshot 并 CAS 推进工作头。"""

    package = await _lock_package(session, support_id, trace_id)
    _check_observed(package, observed_snapshot_digest)
    current = None
    if package.working_snapshot_digest:
        current = await session.scalar(
            select(PackageSnapshot).where(
                PackageSnapshot.package_snapshot_digest == package.working_snapshot_digest
            )
        )
    if current is not None and (
        current.knowledge_snapshot_digest == knowledge_snapshot_digest
        and current.signal_spec_digest == signal_spec_digest
        and current.simulation_spec_digest == simulation_spec_digest
        and current.verification_set_digest == verification_set_digest
        and current.prompt_revision == prompt_revision
        and current.tool_contract_revision == tool_contract_revision
        and current.policy_revision == policy_revision
        and current.compiler_revision == compiler_revision
        and dict(current.manifest_json or {}) == manifest
    ):
        KBD_PACKAGE_SNAPSHOT_TOTAL.labels(result="reused").inc()
        return current
    identity = {
        "support_id": support_id,
        "parent_snapshot_digest": package.working_snapshot_digest,
        "knowledge_snapshot_digest": knowledge_snapshot_digest,
        "signal_spec_digest": signal_spec_digest,
        "simulation_spec_digest": simulation_spec_digest,
        "verification_set_digest": verification_set_digest,
        "prompt_revision": prompt_revision,
        "tool_contract_revision": tool_contract_revision,
        "policy_revision": policy_revision,
        "compiler_revision": compiler_revision,
        "manifest": manifest,
    }
    digest = _digest(identity)
    existing = await session.scalar(select(PackageSnapshot).where(PackageSnapshot.package_snapshot_digest == digest))
    if existing is not None:
        if package.working_snapshot_digest != existing.package_snapshot_digest:
            package.working_snapshot_digest = existing.package_snapshot_digest
            package.workspace_version += 1
            package.status = "draft_editing"
            package.trace_id = trace_id
            package.updated_at = datetime.now(UTC)
            await session.execute(
                update(KbdEntry)
                .where(KbdEntry.support_id == support_id)
                .values(working_snapshot_digest=digest)
            )
        await session.flush()
        KBD_PACKAGE_SNAPSHOT_TOTAL.labels(result="reused").inc()
        return existing

    snapshot = PackageSnapshot(
        package_snapshot_id=uuid4(),
        package_snapshot_digest=digest,
        support_id=support_id,
        parent_snapshot_digest=package.working_snapshot_digest,
        knowledge_snapshot_digest=knowledge_snapshot_digest,
        signal_spec_digest=signal_spec_digest,
        simulation_spec_digest=simulation_spec_digest,
        verification_set_digest=verification_set_digest,
        prompt_revision=prompt_revision,
        tool_contract_revision=tool_contract_revision,
        policy_revision=policy_revision,
        compiler_revision=compiler_revision,
        manifest_json=manifest,
        created_by=actor_id,
        trace_id=trace_id,
    )
    session.add(snapshot)
    await session.flush()
    package.working_snapshot_digest = digest
    package.workspace_version += 1
    package.status = "draft_editing"
    package.trace_id = trace_id
    package.updated_at = datetime.now(UTC)
    await session.execute(
        update(KbdEntry)
        .where(KbdEntry.support_id == support_id)
        .values(working_snapshot_digest=digest)
    )
    await session.flush()
    logger.info(
        event="package_snapshot_created",
        support_id=support_id,
        package_snapshot_digest=digest,
        parent_snapshot_digest=snapshot.parent_snapshot_digest,
        trace_id=trace_id,
    )
    KBD_PACKAGE_SNAPSHOT_TOTAL.labels(result="created").inc()
    return snapshot


async def append_verification_asset(
    session: AsyncSession,
    *,
    support_id: str,
    observed_snapshot_digest: str | None,
    asset: dict[str, Any],
    snapshot_fields: dict[str, str],
    actor_id: str,
    trace_id: str,
) -> tuple[VerificationAsset, PackageSnapshot]:
    """幂等追加验证资产，并在同一事务生成新的集合和工作快照。"""

    package = await _lock_package(session, support_id, trace_id)
    _check_observed(package, observed_snapshot_digest)
    if not package.working_snapshot_digest:
        raise SnapshotConflictError("验证资产必须绑定已存在的工作快照")
    current = await session.scalar(
        select(PackageSnapshot).where(PackageSnapshot.package_snapshot_digest == package.working_snapshot_digest)
    )
    if current is None:
        raise SnapshotConflictError("当前工作快照不存在")
    authoritative_snapshot_fields = {
        "knowledge_snapshot_digest": current.knowledge_snapshot_digest,
        "signal_spec_digest": current.signal_spec_digest,
        "simulation_spec_digest": current.simulation_spec_digest,
        "prompt_revision": current.prompt_revision,
        "tool_contract_revision": current.tool_contract_revision,
        "policy_revision": current.policy_revision,
        "compiler_revision": current.compiler_revision,
    }
    if snapshot_fields != authoritative_snapshot_fields:
        raise SnapshotConflictError("验证资产携带的冻结依赖与当前工作快照不一致")

    declared_digest = str(asset.pop("asset_digest", "") or "")
    asset_digest = verification_asset_digest(support_id, asset, authoritative_snapshot_fields)
    if declared_digest and declared_digest != asset_digest:
        raise VerificationAssetDigestError(
            f"asset_digest mismatch: declared={declared_digest!r}, computed={asset_digest!r}"
        )
    asset["asset_digest"] = asset_digest
    row = await session.scalar(select(VerificationAsset).where(VerificationAsset.asset_digest == asset_digest))
    if row is None:
        row = VerificationAsset(asset_id=uuid4(), support_id=support_id, trace_id=trace_id, **asset)
        session.add(row)
        try:
            async with session.begin_nested():
                await session.flush()
        except IntegrityError:
            row = await session.scalar(select(VerificationAsset).where(VerificationAsset.asset_digest == asset_digest))
            if row is None:
                raise
    elif row.support_id != support_id:
        raise ValueError("asset_digest 已属于其他 support_id")

    current_assets: list[str] = []
    if current.verification_set_digest:
        current_set = await session.scalar(
            select(VerificationSet).where(
                VerificationSet.verification_set_digest == current.verification_set_digest
            )
        )
        if current_set and isinstance(current_set.asset_digests, list):
            current_assets = [str(item) for item in current_set.asset_digests]
    if asset_digest in current_assets:
        KBD_VERIFICATION_ASSET_ATTACH_TOTAL.labels(status=str(row.result_status)).inc()
        return row, current
    current_assets.append(asset_digest)
    current_assets = sorted(set(current_assets))
    set_digest = _digest({"support_id": support_id, "asset_digests": current_assets})
    verification_set = await session.scalar(
        select(VerificationSet).where(VerificationSet.verification_set_digest == set_digest)
    )
    if verification_set is None:
        verification_set = VerificationSet(
            verification_set_id=uuid4(),
            verification_set_digest=set_digest,
            support_id=support_id,
            asset_digests=current_assets,
            asset_count=len(current_assets),
            created_by=actor_id,
            trace_id=trace_id,
        )
        session.add(verification_set)
        await session.flush()

    snapshot = await create_snapshot(
        session,
        support_id=support_id,
        observed_snapshot_digest=package.working_snapshot_digest,
        verification_set_digest=set_digest,
        manifest={
            **dict(current.manifest_json or {}),
            "verification_asset_digest": asset_digest,
            "asset_count": len(current_assets),
        },
        actor_id=actor_id,
        trace_id=trace_id,
        **snapshot_fields,
    )
    KBD_VERIFICATION_ASSET_ATTACH_TOTAL.labels(status=str(row.result_status)).inc()
    return row, snapshot


async def ensure_publish_snapshot(
    session: AsyncSession,
    *,
    kbd: KbdEntry,
    trace_id: str,
) -> PackageSnapshot:
    """发布前把当前 KBD 投影收敛为一个可复现的 PackageSnapshot。"""

    from app.services.kbd_revision_service import build_kbd_revision_payload

    knowledge_payload = build_kbd_revision_payload(kbd)
    signals = knowledge_payload.get("signals_json") if isinstance(knowledge_payload, dict) else {}
    publish_validation = signals.get("publish_validation") if isinstance(signals, dict) else {}
    knowledge_digest = _digest(knowledge_payload)
    signal_digest = _digest(signals or {})
    simulation_digest = _digest({"support_id": kbd.support_id, "assets": []})
    prompt_revision = str((publish_validation or {}).get("prompt_revision") or "legacy-unversioned")
    tool_revision = str((publish_validation or {}).get("tool_contract_revision") or "legacy-unversioned")
    policy_revision = str((publish_validation or {}).get("policy_revision") or "legacy-unversioned")
    compiler_revision = str((publish_validation or {}).get("compiler_revision") or "legacy-unversioned")
    source_revision_no = None
    working_revision_id = getattr(kbd, "working_revision_id", None)
    if working_revision_id is not None:
        from app.models.kbd_revision import KbdRevision

        working_revision = await session.get(KbdRevision, working_revision_id)
        if working_revision is not None:
            source_revision_no = int(working_revision.revision_no)

    package = await _lock_package(session, str(kbd.support_id), trace_id)
    current = None
    if package.working_snapshot_digest:
        current = await session.scalar(
            select(PackageSnapshot).where(
                PackageSnapshot.package_snapshot_digest == package.working_snapshot_digest
            )
        )
    expected = (
        knowledge_digest,
        signal_digest,
        simulation_digest,
        prompt_revision,
        tool_revision,
        policy_revision,
        compiler_revision,
    )
    actual = (
        current.knowledge_snapshot_digest,
        current.signal_spec_digest,
        current.simulation_spec_digest,
        current.prompt_revision,
        current.tool_contract_revision,
        current.policy_revision,
        current.compiler_revision,
    ) if current is not None else None
    if actual == expected:
        return current

    return await create_snapshot(
        session,
        support_id=str(kbd.support_id),
        observed_snapshot_digest=package.working_snapshot_digest,
        knowledge_snapshot_digest=knowledge_digest,
        signal_spec_digest=signal_digest,
        simulation_spec_digest=simulation_digest,
        verification_set_digest=None,
        prompt_revision=prompt_revision,
        tool_contract_revision=tool_revision,
        policy_revision=policy_revision,
        compiler_revision=compiler_revision,
        manifest={
            "source": "kbd_publish",
            "kbd_id": int(kbd.id),
            "knowledge_revision_id": int(working_revision_id) if working_revision_id else None,
            "source_knowledge_revision_no": source_revision_no,
            "content_digest": knowledge_digest,
        },
        actor_id="system:kbd-publisher",
        trace_id=trace_id,
    )


async def advance_revision_snapshot(
    session: AsyncSession,
    *,
    kbd: KbdEntry,
    revision_id: int,
    revision_no: int,
    payload: dict[str, Any],
    actor_type: str,
    trace_id: str | None,
) -> PackageSnapshot:
    """将不可变 KbdRevision 原子投影为新的 Package 工作头。"""

    signals = payload.get("signals_json") if isinstance(payload, dict) else {}
    publish_validation = signals.get("publish_validation") if isinstance(signals, dict) else {}
    package = await _lock_package(session, str(kbd.support_id), trace_id or "trace-unavailable")
    current = None
    if package.working_snapshot_digest:
        current = await session.scalar(
            select(PackageSnapshot).where(
                PackageSnapshot.package_snapshot_digest == package.working_snapshot_digest
            )
        )
    if current is not None and int((current.manifest_json or {}).get("knowledge_revision_id") or 0) == revision_id:
        return current
    return await create_snapshot(
        session,
        support_id=str(kbd.support_id),
        observed_snapshot_digest=package.working_snapshot_digest,
        knowledge_snapshot_digest=_digest(payload),
        signal_spec_digest=_digest(signals or {}),
        simulation_spec_digest=_digest({"support_id": kbd.support_id, "assets": []}),
        verification_set_digest=None,
        prompt_revision=str((publish_validation or {}).get("prompt_revision") or "legacy-unversioned"),
        tool_contract_revision=str(
            (publish_validation or {}).get("tool_contract_revision") or "legacy-unversioned"
        ),
        policy_revision=str((publish_validation or {}).get("policy_revision") or "legacy-unversioned"),
        compiler_revision=str((publish_validation or {}).get("compiler_revision") or "legacy-unversioned"),
        manifest={
            "source": "kbd_revision",
            "kbd_id": int(kbd.id),
            "knowledge_revision_id": revision_id,
            "source_knowledge_revision_no": revision_no,
        },
        actor_id=f"{actor_type}:kbd-revision",
        trace_id=trace_id or "trace-unavailable",
    )
