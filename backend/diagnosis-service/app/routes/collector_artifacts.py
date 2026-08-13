"""Collector Artifact（采集器制品）API。"""

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Response

from app.auth import ActorContext, require_actor
from app.dependencies import get_collector_artifact_service, get_collector_trust_service
from app.errors import DiagnosisError
from app.schemas.collector_artifact import (
    CollectorArtifactGenerateRequest,
    CollectorArtifactResponse,
    CollectorArtifactRevokeRequest,
)
from app.services.collector_artifact_service import CollectorArtifactService
from app.services.collector_trust_service import CollectorTrustService

router = APIRouter(prefix="/api/diagnosis-sessions/{session_id}/collector-artifacts", tags=["collector-artifacts"])
management_router = APIRouter(prefix="/api/internal/collector-artifacts", tags=["collector-artifact-management"])


@management_router.get("", response_model=list[CollectorArtifactResponse])
async def list_collector_artifacts(
    actor: Annotated[ActorContext, Depends(require_actor)],
    service: Annotated[CollectorArtifactService, Depends(get_collector_artifact_service)],
    status: Annotated[str | None, Query()] = None,
    session_id: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> list[CollectorArtifactResponse]:
    """列出租户内 Collector Artifact（采集器制品）。"""

    results = await service.list_managed(actor=actor, status=status, session_id=session_id, limit=limit)
    return [CollectorArtifactResponse.from_entities(item.entity, item.items) for item in results]


@management_router.post("/{artifact_id}/revoke", response_model=CollectorArtifactResponse)
async def revoke_managed_collector_artifact(
    artifact_id: UUID,
    actor: Annotated[ActorContext, Depends(require_actor)],
    service: Annotated[CollectorArtifactService, Depends(get_collector_artifact_service)],
    body: CollectorArtifactRevokeRequest | None = None,
) -> CollectorArtifactResponse:
    """管理端按制品 ID 撤销 Collector Artifact（采集器制品）。"""

    result = await service.revoke_managed(
        actor=actor,
        artifact_id=str(artifact_id),
        command=body or CollectorArtifactRevokeRequest(),
    )
    return CollectorArtifactResponse.from_entities(result.entity, result.items)


@router.post("", response_model=CollectorArtifactResponse, status_code=201)
async def generate_collector_artifact(
    session_id: UUID,
    body: CollectorArtifactGenerateRequest,
    response: Response,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)],
    actor: Annotated[ActorContext, Depends(require_actor)],
    service: Annotated[CollectorArtifactService, Depends(get_collector_artifact_service)],
) -> CollectorArtifactResponse:
    """生成签名结构化 Collector Artifact（采集器制品）。"""

    result = await service.generate(
        actor=actor,
        session_id=str(session_id),
        command=body,
        idempotency_key=idempotency_key,
    )
    response.headers["Idempotent-Replayed"] = "false" if result.created else "true"
    return CollectorArtifactResponse.from_entities(result.entity, result.items)


@router.get("/{artifact_id}", response_model=CollectorArtifactResponse)
async def get_collector_artifact(
    session_id: UUID,
    artifact_id: UUID,
    actor: Annotated[ActorContext, Depends(require_actor)],
    service: Annotated[CollectorArtifactService, Depends(get_collector_artifact_service)],
) -> CollectorArtifactResponse:
    """读取制品元数据。"""

    result = await service.get(actor=actor, session_id=str(session_id), artifact_id=str(artifact_id))
    return CollectorArtifactResponse.from_entities(result.entity, result.items)


@router.get("/{artifact_id}/download")
async def download_collector_artifact(
    session_id: UUID,
    artifact_id: UUID,
    actor: Annotated[ActorContext, Depends(require_actor)],
    service: Annotated[CollectorArtifactService, Depends(get_collector_artifact_service)],
) -> Response:
    """下载已签名结构化采集制品，签名通过响应头旁路返回。"""

    result = await service.get(actor=actor, session_id=str(session_id), artifact_id=str(artifact_id))
    artifact = result.entity
    if artifact.status != "ready" or artifact.expires_at <= datetime.now(UTC):
        raise DiagnosisError(
            code="COLLECTOR_ARTIFACT_EXPIRED",
            message="采集器制品已过期或已撤销",
            http_status=410,
        )
    return Response(
        content=artifact.content_text,
        media_type="application/vnd.hci.collector+json",
        headers={
            "Content-Disposition": f'attachment; filename="{artifact.file_name}"',
            "Cache-Control": "private, no-store",
            "X-Artifact-SHA256": artifact.artifact_sha256,
            "X-Signature-Algorithm": artifact.signature_algorithm,
            "X-Signature-Key-ID": artifact.signing_key_id,
            "X-Detached-Signature": artifact.signature_base64,
            "X-Public-Key-Base64": artifact.public_key_base64,
            "X-Public-Key-Fingerprint": artifact.public_key_fingerprint,
        },
    )


@router.get("/{artifact_id}/verification-bundle")
async def download_collector_verification_bundle(
    session_id: UUID,
    artifact_id: UUID,
    actor: Annotated[ActorContext, Depends(require_actor)],
    service: Annotated[CollectorTrustService, Depends(get_collector_trust_service)],
) -> Response:
    """下载含受信根、吊销快照和离线验证器的 ZIP。"""

    bundle = await service.build_verification_bundle(
        actor=actor,
        session_id=str(session_id),
        artifact_id=str(artifact_id),
    )
    return Response(
        content=bundle.content,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{bundle.file_name}"',
            "Cache-Control": "private, no-store",
            "X-Root-Public-Key-Fingerprint": bundle.root_fingerprint,
            "X-Revocation-Next-Update": bundle.revocation_next_update_at.isoformat(),
            "X-Verification-Bundle-SHA256": bundle.bundle_sha256,
        },
    )


@router.post("/{artifact_id}/revoke", response_model=CollectorArtifactResponse)
async def revoke_collector_artifact(
    session_id: UUID,
    artifact_id: UUID,
    actor: Annotated[ActorContext, Depends(require_actor)],
    service: Annotated[CollectorArtifactService, Depends(get_collector_artifact_service)],
    body: CollectorArtifactRevokeRequest | None = None,
) -> CollectorArtifactResponse:
    """撤销采集器制品。"""

    result = await service.revoke(
        actor=actor,
        session_id=str(session_id),
        artifact_id=str(artifact_id),
        command=body or CollectorArtifactRevokeRequest(),
    )
    return CollectorArtifactResponse.from_entities(result.entity, result.items)
