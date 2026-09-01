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
from app.models.version_governance import KbdPackage, PackageSnapshot, VerificationAsset

logger = get_logger("kb-service-version-governance")


class SnapshotConflictError(Exception):
    """工作区已被其他请求推进。"""


class VerificationAssetDigestError(ValueError):
    """验证资产的声明摘要与服务端规范化内容不一致。"""


def _digest(value: Any) -> str:
    serialized = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return f"sha256:{hashlib.sha256(serialized.encode('utf-8')).hexdigest()}"


def _normalize_asset_payload(asset: dict[str, Any]) -> dict[str, Any]:
    return {
        "signal_id": str(asset["signal_id"]),
        "processing_index": int(asset["processing_index"]),
        "dataset_id": str(asset["dataset_id"]),
        "input_digest": str(asset["input_digest"]),
        "deterministic_input": dict(asset.get("deterministic_input") or {}),
        "ai_input": dict(asset.get("ai_input") or {}),
        "raw_response_hash": asset.get("raw_response_hash"),
        "output_json": dict(asset.get("output_json") or {}),
        "evidence_json": dict(asset.get("evidence_json") or {}),
        "downstream_result": dict(asset.get("downstream_result") or {}),
        "model": str(asset["model"]),
        "prompt_revision": str(asset["prompt_revision"]),
        "contract_version": str(asset["contract_version"]),
        "run_id": asset.get("run_id"),
        "result_status": str(asset["result_status"]),
    }


def verification_asset_digest(
    support_id: str,
    asset: dict[str, Any],
) -> str:
    """计算单个验证资产的不可变 SHA-256。"""

    normalized = _normalize_asset_payload(asset)
    identity = {
        "support_id": support_id,
        **normalized,
    }
    return _digest(identity)


async def _lock_package(
    session: AsyncSession,
    support_id: str,
    trace_id: str,
) -> KbdPackage:
    stmt = (
        pg_insert(KbdPackage)
        .values(
            package_id=uuid4(),
            support_id=support_id,
            status="draft_editing",
            workspace_version=1,
            trace_id=trace_id,
        )
        .on_conflict_do_nothing(index_elements=["support_id"])
    )
    await session.execute(stmt)
    query = select(KbdPackage).where(KbdPackage.support_id == support_id).with_for_update()
    package = await session.scalar(query)
    if package is None:
        raise RuntimeError(f"kbd_package row missing after upsert: support_id={support_id}")
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
    verification_assets: list[str] | None = None,
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
    clean_assets = sorted(set(verification_assets or []))
    if current is not None and (
        current.knowledge_snapshot_digest == knowledge_snapshot_digest
        and current.signal_spec_digest == signal_spec_digest
        and current.simulation_spec_digest == simulation_spec_digest
        and list(current.verification_assets or []) == clean_assets
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
        "verification_assets": clean_assets,
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
        verification_assets=clean_assets,
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
    actor_id: str,
    trace_id: str,
    declared_digest: str | None = None,
    **snapshot_fields: Any,
) -> tuple[VerificationAsset, PackageSnapshot]:
    """保存单次试运行不可变证据并原子追加到当前 PackageSnapshot 中。"""

    package = await _lock_package(session, support_id, trace_id)
    _check_observed(package, observed_snapshot_digest)
    current = None
    if package.working_snapshot_digest:
        current = await session.scalar(
            select(PackageSnapshot).where(
                PackageSnapshot.package_snapshot_digest == package.working_snapshot_digest
            )
        )
    if current is None:
        raise ValueError(f"当前 working_snapshot_digest={package.working_snapshot_digest!r} 对应快照不存在")

    asset = _normalize_asset_payload(asset)
    asset_digest = verification_asset_digest(support_id, asset)
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

    current_assets: list[str] = list(current.verification_assets or []) if isinstance(current.verification_assets, list) else []
    if asset_digest in current_assets:
        KBD_VERIFICATION_ASSET_ATTACH_TOTAL.labels(status=str(row.result_status)).inc()
        return row, current
    current_assets.append(asset_digest)
    current_assets = sorted(set(current_assets))

    snapshot = await create_snapshot(
        session,
        support_id=support_id,
        observed_snapshot_digest=package.working_snapshot_digest,
        verification_assets=current_assets,
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
        verification_assets=[],
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
        verification_assets=[],
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
