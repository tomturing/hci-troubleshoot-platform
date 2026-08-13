"""Collector（采集器）离线信任根和吊销清单 API。"""

from typing import Annotated

from fastapi import APIRouter, Depends, Response

from app.auth import ActorContext, require_actor
from app.dependencies import get_collector_trust_service
from app.schemas.collector_trust import CollectorRevocationListResponse, CollectorTrustStoreResponse
from app.services.collector_trust_service import CollectorTrustService

router = APIRouter(prefix="/api/internal/collectors/security", tags=["collector-trust"])


@router.get("/trust-store", response_model=CollectorTrustStoreResponse)
async def get_collector_trust_store(
    response: Response,
    actor: Annotated[ActorContext, Depends(require_actor)],
    service: Annotated[CollectorTrustService, Depends(get_collector_trust_service)],
) -> CollectorTrustStoreResponse:
    """读取当前 P0 单密钥受信根。"""

    response.headers["Cache-Control"] = "private, no-store"
    return CollectorTrustStoreResponse.model_validate(service.trust_store(actor=actor))


@router.get("/revocations", response_model=CollectorRevocationListResponse)
async def get_collector_revocations(
    response: Response,
    actor: Annotated[ActorContext, Depends(require_actor)],
    service: Annotated[CollectorTrustService, Depends(get_collector_trust_service)],
) -> CollectorRevocationListResponse:
    """读取租户范围内的短时有效签名吊销清单。"""

    response.headers["Cache-Control"] = "private, no-store"
    return CollectorRevocationListResponse.model_validate(await service.revocation_list(actor=actor))
