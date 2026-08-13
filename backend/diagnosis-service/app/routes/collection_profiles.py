"""Collection Profile（采集画像）内部 API。"""

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Response

from app.auth import ActorContext, require_actor
from app.dependencies import get_collection_profile_service
from app.schemas.collection_profile import (
    CollectionProfilePublishRequest,
    CollectionProfileResponse,
    CollectionProfileReviewRequest,
    OfflineScenarioOptionResponse,
)
from app.services.collection_profile_service import CollectionProfileService

router = APIRouter(prefix="/api/internal/collection-profiles", tags=["collection-profiles"])
scenario_router = APIRouter(prefix="/api/diagnosis-scenarios", tags=["diagnosis-scenarios"])


@scenario_router.get("", response_model=list[OfflineScenarioOptionResponse])
async def list_available_diagnosis_scenarios(
    actor: Annotated[ActorContext, Depends(require_actor)],
    service: Annotated[CollectionProfileService, Depends(get_collection_profile_service)],
) -> list[OfflineScenarioOptionResponse]:
    """只返回客户当前可以选择的已发布 Collection Profile（采集画像）场景。"""

    return await service.list_available_scenarios(actor=actor)


@router.get("", response_model=list[CollectionProfileResponse])
async def list_active_collection_profiles(
    actor: Annotated[ActorContext, Depends(require_actor)],
    service: Annotated[CollectionProfileService, Depends(get_collection_profile_service)],
) -> list[CollectionProfileResponse]:
    """列出全部当前生效的 Collection Profile（采集画像）快照。"""

    return await service.list_active(actor=actor)


@router.post("/{profile_id}/revisions", response_model=CollectionProfileResponse, status_code=201)
async def publish_collection_profile(
    profile_id: str,
    body: CollectionProfilePublishRequest,
    actor: Annotated[ActorContext, Depends(require_actor)],
    service: Annotated[CollectionProfileService, Depends(get_collection_profile_service)],
) -> CollectionProfileResponse:
    """发布不可变画像修订版本并切换生效指针。"""

    return await service.publish(actor=actor, profile_id=profile_id, command=body)


@router.put("/{profile_id}", response_model=CollectionProfileResponse)
async def save_collection_profile_draft(
    profile_id: str,
    body: CollectionProfilePublishRequest,
    response: Response,
    actor: Annotated[ActorContext, Depends(require_actor)],
    service: Annotated[CollectionProfileService, Depends(get_collection_profile_service)],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> CollectionProfileResponse:
    """创建或更新 Collection Profile（采集画像）草稿。"""

    result = await service.save_draft(
        actor=actor,
        profile_id=profile_id,
        command=body,
        if_match=if_match,
    )
    response.headers["ETag"] = f'"{result.lock_version}"'
    return result


@router.post("/{profile_id}/review", response_model=CollectionProfileResponse)
async def review_collection_profile(
    profile_id: str,
    body: CollectionProfileReviewRequest,
    response: Response,
    actor: Annotated[ActorContext, Depends(require_actor)],
    service: Annotated[CollectionProfileService, Depends(get_collection_profile_service)],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> CollectionProfileResponse:
    """批准或拒绝 Collection Profile（采集画像）。"""

    result = await service.review(
        actor=actor,
        profile_id=profile_id,
        command=body,
        if_match=if_match,
    )
    response.headers["ETag"] = f'"{result.lock_version}"'
    return result


@router.post("/{profile_id}/disable", response_model=CollectionProfileResponse)
async def disable_collection_profile(
    profile_id: str,
    response: Response,
    actor: Annotated[ActorContext, Depends(require_actor)],
    service: Annotated[CollectionProfileService, Depends(get_collection_profile_service)],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> CollectionProfileResponse:
    """禁用 Collection Profile（采集画像）。"""

    result = await service.disable(actor=actor, profile_id=profile_id, if_match=if_match)
    response.headers["ETag"] = f'"{result.lock_version}"'
    return result


@router.get("/{profile_id}", response_model=CollectionProfileResponse)
async def get_active_collection_profile(
    profile_id: str,
    response: Response,
    actor: Annotated[ActorContext, Depends(require_actor)],
    service: Annotated[CollectionProfileService, Depends(get_collection_profile_service)],
) -> CollectionProfileResponse:
    """读取当前生效画像。"""

    result = await service.get_active(actor=actor, profile_id=profile_id)
    response.headers["ETag"] = f'"{result.lock_version}"'
    return result
