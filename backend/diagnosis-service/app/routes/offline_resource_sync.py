"""KBD 驱动的 Collector/Profile 同步、历史和回滚 API。"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.auth import ActorContext, require_actor
from app.dependencies import get_offline_resource_sync_service
from app.schemas.offline_resource_sync import (
    OfflineResourceSyncBatchResponse,
    OfflineResourceSyncDecisionRequest,
    OfflineResourceSyncHistoryResponse,
    OfflineResourceSyncPreviewRequest,
)
from app.services.offline_resource_sync_service import OfflineResourceSyncService

router = APIRouter(prefix="/api/internal/offline-resource-sync", tags=["offline-resource-sync"])


@router.post("/preview", response_model=OfflineResourceSyncBatchResponse, status_code=201)
async def preview_offline_resource_sync(
    body: OfflineResourceSyncPreviewRequest,
    actor: Annotated[ActorContext, Depends(require_actor)],
    service: Annotated[OfflineResourceSyncService, Depends(get_offline_resource_sync_service)],
) -> OfflineResourceSyncBatchResponse:
    """读取上次游标后的 KBD 修订并保存候选差异，不改变生效资源。"""

    return OfflineResourceSyncBatchResponse.model_validate(await service.preview(actor=actor, mode=body.mode))


@router.get("/history", response_model=OfflineResourceSyncHistoryResponse)
async def list_offline_resource_sync_history(
    actor: Annotated[ActorContext, Depends(require_actor)],
    service: Annotated[OfflineResourceSyncService, Depends(get_offline_resource_sync_service)],
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> OfflineResourceSyncHistoryResponse:
    """查询所有同步批次；预检、失败、拒绝、发布和回滚结果均可追溯。"""

    return OfflineResourceSyncHistoryResponse.model_validate(
        await service.list_history(actor=actor, offset=offset, limit=limit)
    )


@router.get("/{batch_id}", response_model=OfflineResourceSyncBatchResponse)
async def get_offline_resource_sync_batch(
    batch_id: str,
    actor: Annotated[ActorContext, Depends(require_actor)],
    service: Annotated[OfflineResourceSyncService, Depends(get_offline_resource_sync_service)],
) -> OfflineResourceSyncBatchResponse:
    """读取批次资源差异与追加式动作历史。"""

    return OfflineResourceSyncBatchResponse.model_validate(await service.get(actor=actor, batch_id=batch_id))


@router.post("/{batch_id}/publish", response_model=OfflineResourceSyncBatchResponse)
async def publish_offline_resource_sync_batch(
    batch_id: str,
    body: OfflineResourceSyncDecisionRequest,
    actor: Annotated[ActorContext, Depends(require_actor)],
    service: Annotated[OfflineResourceSyncService, Depends(get_offline_resource_sync_service)],
) -> OfflineResourceSyncBatchResponse:
    """经管理员确认后原子发布整个候选批次。"""

    return OfflineResourceSyncBatchResponse.model_validate(
        await service.publish(actor=actor, batch_id=batch_id, reason=body.reason)
    )


@router.post("/{batch_id}/reject", response_model=OfflineResourceSyncBatchResponse)
async def reject_offline_resource_sync_batch(
    batch_id: str,
    body: OfflineResourceSyncDecisionRequest,
    actor: Annotated[ActorContext, Depends(require_actor)],
    service: Annotated[OfflineResourceSyncService, Depends(get_offline_resource_sync_service)],
) -> OfflineResourceSyncBatchResponse:
    """拒绝候选批次但永久保留差异和原因。"""

    return OfflineResourceSyncBatchResponse.model_validate(
        await service.reject(actor=actor, batch_id=batch_id, reason=body.reason)
    )


@router.post("/{batch_id}/rollback", response_model=OfflineResourceSyncBatchResponse)
async def rollback_offline_resource_sync_batch(
    batch_id: str,
    body: OfflineResourceSyncDecisionRequest,
    actor: Annotated[ActorContext, Depends(require_actor)],
    service: Annotated[OfflineResourceSyncService, Depends(get_offline_resource_sync_service)],
) -> OfflineResourceSyncBatchResponse:
    """原子回滚最后一次已发布批次并恢复 KBD 同步游标。"""

    return OfflineResourceSyncBatchResponse.model_validate(
        await service.rollback(actor=actor, batch_id=batch_id, reason=body.reason)
    )
