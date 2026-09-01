"""KBD Package 工作区与验证资产 API。

接口显式携带快照 digest，服务端在事务内执行 CAS，拒绝基于过期工作区的写入。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field
from shared.models.dynamic_resource import DynamicResourceRevision
from shared.observability.logger import get_logger
from shared.observability.otel import get_current_trace_id
from sqlalchemy import select

from app.models.kbd_entry import KbdEntry
from app.models.kbd_revision import KbdRevision
from app.models.version_governance import KbdPackage, PackageSnapshot
from app.services.version_governance import (
    SnapshotConflictError,
    VerificationAssetDigestError,
    append_verification_asset,
    create_snapshot,
)

if TYPE_CHECKING:
    from shared.database.postgres import DatabaseManager

router = APIRouter(prefix="/api/v1/kbd", tags=["kbd-version-governance"])
logger = get_logger("kb-service-version-governance-api")
_db_manager: DatabaseManager | None = None
_SHA256_PATTERN = r"^sha256:[0-9a-f]{64}$"


def set_dependencies(db: DatabaseManager) -> None:
    """由应用生命周期注入数据库。"""

    global _db_manager
    _db_manager = db


def _check_auth(request: Request) -> None:
    """校验内部服务身份。"""

    from app.config import settings

    if request.headers.get("Authorization") != f"Bearer {settings.INTERNAL_API_TOKEN}":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="内部身份认证失败")


class SnapshotRequest(BaseModel):
    """创建工作快照请求。"""

    observed_snapshot_digest: str | None = None
    knowledge_snapshot_digest: str = Field(..., pattern=_SHA256_PATTERN)
    signal_spec_digest: str = Field(..., pattern=_SHA256_PATTERN)
    simulation_spec_digest: str = Field(..., pattern=_SHA256_PATTERN)
    verification_assets: list[str] = Field(default_factory=list)
    prompt_revision: str = Field(..., min_length=1, max_length=128)
    tool_contract_revision: str = Field(..., min_length=1, max_length=128)
    policy_revision: str = Field(..., min_length=1, max_length=128)
    compiler_revision: str = Field(..., min_length=1, max_length=128)
    manifest: dict[str, Any] = Field(default_factory=dict)
    actor_id: str = Field(..., min_length=1, max_length=128)


class VerificationAssetRequest(BaseModel):
    """保存一次不可变验证结果。"""

    observed_snapshot_digest: str | None = None
    asset_digest: str | None = Field(None, pattern=_SHA256_PATTERN)
    signal_id: str = Field(..., min_length=1, max_length=128)
    processing_index: int = Field(..., ge=0)
    dataset_id: str = Field(..., min_length=1, max_length=128)
    input_digest: str = Field(..., pattern=_SHA256_PATTERN)
    deterministic_input: dict[str, Any] = Field(default_factory=dict)
    ai_input: dict[str, Any] = Field(default_factory=dict)
    raw_response_hash: str | None = None
    output_json: dict[str, Any] = Field(default_factory=dict)
    evidence_json: dict[str, Any] = Field(default_factory=dict)
    downstream_result: dict[str, Any] = Field(default_factory=dict)
    model: str = Field(..., min_length=1, max_length=128)
    prompt_revision: str = Field(..., min_length=1, max_length=128)
    contract_version: str = Field(..., min_length=1, max_length=128)
    run_id: str | None = None
    result_status: str = Field(..., pattern="^(pass|fail|inconclusive)$")
    knowledge_snapshot_digest: str = Field(..., pattern=_SHA256_PATTERN)
    signal_spec_digest: str = Field(..., pattern=_SHA256_PATTERN)
    simulation_spec_digest: str = Field(..., pattern=_SHA256_PATTERN)
    tool_contract_revision: str = Field(..., min_length=1, max_length=128)
    policy_revision: str = Field(..., min_length=1, max_length=128)
    compiler_revision: str = Field(..., min_length=1, max_length=128)
    actor_id: str = Field(..., min_length=1, max_length=128)


def _context_payload(
    package: KbdPackage,
    snapshot: PackageSnapshot | None,
    scope: str,
    trace_id: str,
    knowledge_release_id: str | None = None,
    source_knowledge_revision_no: int | None = None,
) -> dict[str, Any]:
    return {
        "support_id": package.support_id,
        "scope": scope,
        "package_id": str(package.package_id),
        "package_snapshot_digest": snapshot.package_snapshot_digest if snapshot else None,
        "knowledge_snapshot_digest": snapshot.knowledge_snapshot_digest if snapshot else None,
        "signal_spec_digest": snapshot.signal_spec_digest if snapshot else None,
        "simulation_spec_digest": snapshot.simulation_spec_digest if snapshot else None,
        "prompt_revision": snapshot.prompt_revision if snapshot else None,
        "tool_contract_revision": snapshot.tool_contract_revision if snapshot else None,
        "policy_revision": snapshot.policy_revision if snapshot else None,
        "compiler_revision": snapshot.compiler_revision if snapshot else None,
        "knowledge_release_id": knowledge_release_id,
        "bundle_build_id": None,
        "workspace_version": package.workspace_version,
        "source_knowledge_revision_no": source_knowledge_revision_no,
        "trace_id": trace_id,
    }


@router.get("/{support_id}/context")
async def get_context(support_id: str, request: Request, scope: str = "working_draft") -> dict[str, Any]:
    """读取当前工作快照或已发布上下文。"""

    _check_auth(request)
    if scope not in {"working_draft", "active_release"}:
        raise HTTPException(status_code=400, detail="scope 必须是 working_draft 或 active_release")
    if _db_manager is None:
        raise HTTPException(status_code=503, detail="服务未就绪")
    trace_id = get_current_trace_id()
    async with _db_manager.async_session_factory() as session:
        package = await session.scalar(select(KbdPackage).where(KbdPackage.support_id == support_id))
        if package is None:
            raise HTTPException(status_code=404, detail="KbdPackage 不存在")
        digest = package.working_snapshot_digest
        knowledge_release_id = None
        source_revision_no = None
        if scope == "active_release":
            if package.active_release_id is None:
                raise HTTPException(status_code=404, detail="当前没有 active release")
            release = await session.get(DynamicResourceRevision, package.active_release_id)
            digest = release.package_snapshot_digest if release is not None else None
            knowledge_release_id = str(release.release_id) if release is not None and release.release_id else None
            source_revision_no = int(release.revision) if release is not None else None
            if not digest or not knowledge_release_id:
                raise HTTPException(status_code=404, detail="active release 缺少统一版本身份")
        snapshot = await session.scalar(
            select(PackageSnapshot).where(PackageSnapshot.package_snapshot_digest == digest)
        ) if digest else None
        if scope == "active_release" and snapshot is not None:
            source_revision_no = int(
                (snapshot.manifest_json or {}).get("source_knowledge_revision_no") or source_revision_no or 0
            ) or None
        if scope == "working_draft" and snapshot is not None:
            source_revision_no = int((snapshot.manifest_json or {}).get("source_knowledge_revision_no") or 0) or None
            if source_revision_no is None:
                entry = await session.scalar(select(KbdEntry).where(KbdEntry.support_id == support_id))
                if entry is not None and entry.working_revision_id is not None:
                    revision = await session.get(KbdRevision, entry.working_revision_id)
                    source_revision_no = int(revision.revision_no) if revision is not None else None
        return _context_payload(
            package,
            snapshot,
            scope,
            trace_id,
            knowledge_release_id,
            source_revision_no,
        )


@router.post("/{support_id}/working-draft/snapshots", status_code=status.HTTP_201_CREATED)
async def save_snapshot(support_id: str, body: SnapshotRequest, request: Request) -> dict[str, Any]:
    """保存工作快照；观察到的 digest 过期时返回 409。"""

    _check_auth(request)
    if _db_manager is None:
        raise HTTPException(status_code=503, detail="服务未就绪")
    trace_id = get_current_trace_id()
    async with _db_manager.async_session_factory() as session:
        try:
            snapshot = await create_snapshot(
                session,
                support_id=support_id,
                observed_snapshot_digest=body.observed_snapshot_digest,
                knowledge_snapshot_digest=body.knowledge_snapshot_digest,
                signal_spec_digest=body.signal_spec_digest,
                simulation_spec_digest=body.simulation_spec_digest,
                verification_assets=body.verification_assets,
                prompt_revision=body.prompt_revision,
                tool_contract_revision=body.tool_contract_revision,
                policy_revision=body.policy_revision,
                compiler_revision=body.compiler_revision,
                manifest=body.manifest,
                actor_id=body.actor_id,
                trace_id=trace_id,
            )
            await session.commit()
        except SnapshotConflictError as exc:
            await session.rollback()
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"support_id": support_id, "package_snapshot_digest": snapshot.package_snapshot_digest, "trace_id": trace_id}


@router.post("/{support_id}/working-draft/verification-assets", status_code=status.HTTP_201_CREATED)
async def save_verification_asset(support_id: str, body: VerificationAssetRequest, request: Request) -> dict[str, Any]:
    """原子保存验证资产、验证集合和新的工作快照。"""

    _check_auth(request)
    if _db_manager is None:
        raise HTTPException(status_code=503, detail="服务未就绪")
    trace_id = get_current_trace_id()
    asset_payload = body.model_dump(
        include={
            "asset_digest", "signal_id", "processing_index", "dataset_id", "input_digest",
            "deterministic_input", "ai_input", "raw_response_hash", "output_json", "evidence_json",
            "downstream_result", "model", "prompt_revision", "contract_version", "run_id", "result_status",
        }
    )
    snapshot_fields = body.model_dump(
        include={
            "knowledge_snapshot_digest", "signal_spec_digest", "simulation_spec_digest",
            "tool_contract_revision", "policy_revision", "compiler_revision", "prompt_revision",
        }
    )
    async with _db_manager.async_session_factory() as session:
        try:
            asset, snapshot = await append_verification_asset(
                session,
                support_id=support_id,
                observed_snapshot_digest=body.observed_snapshot_digest,
                asset=asset_payload,
                snapshot_fields=snapshot_fields,
                actor_id=body.actor_id,
                trace_id=trace_id,
            )
            await session.commit()
        except SnapshotConflictError as exc:
            await session.rollback()
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except VerificationAssetDigestError as exc:
            await session.rollback()
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    logger.info(
        event="verification_asset_attached",
        support_id=support_id,
        verification_asset_id=str(asset.asset_id),
        package_snapshot_digest=snapshot.package_snapshot_digest,
        trace_id=trace_id,
    )
    return {
        "support_id": support_id,
        "verification_asset_id": str(asset.asset_id),
        "asset_digest": asset.asset_digest,
        "package_snapshot_digest": snapshot.package_snapshot_digest,
        "trace_id": trace_id,
    }
