"""Collection Plan（采集计划）API。"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Response

from app.auth import ActorContext, require_actor
from app.dependencies import get_collection_plan_service
from app.schemas.collection_plan import (
    CollectionPlanGenerateRequest,
    CollectionPlanRegenerateRequest,
    CollectionPlanResponse,
)
from app.services.collection_plan_service import CollectionPlanService

router = APIRouter(prefix="/api/diagnosis-sessions/{session_id}/collection-plans", tags=["collection-plans"])
management_router = APIRouter(prefix="/api/internal/collection-plans", tags=["collection-plan-management"])


@router.post("", response_model=CollectionPlanResponse, status_code=201)
async def generate_collection_plan(
    session_id: UUID,
    body: CollectionPlanGenerateRequest,
    response: Response,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)],
    actor: Annotated[ActorContext, Depends(require_actor)],
    service: Annotated[CollectionPlanService, Depends(get_collection_plan_service)],
) -> CollectionPlanResponse:
    """生成初始采集计划。"""

    result = await service.generate(
        actor=actor,
        session_id=str(session_id),
        command=body,
        idempotency_key=idempotency_key,
    )
    response.headers["Idempotent-Replayed"] = "false" if result.created else "true"
    return CollectionPlanResponse.from_entities(result.entity, result.items)


@management_router.get("", response_model=list[CollectionPlanResponse])
async def list_collection_plans(
    actor: Annotated[ActorContext, Depends(require_actor)],
    service: Annotated[CollectionPlanService, Depends(get_collection_plan_service)],
    status: Annotated[str | None, Query()] = None,
    session_id: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> list[CollectionPlanResponse]:
    """列出租户内 Collection Plan（采集计划）。"""

    results = await service.list_managed(actor=actor, status=status, session_id=session_id, limit=limit)
    return [CollectionPlanResponse.from_entities(item.entity, item.items) for item in results]


@management_router.post("/{plan_id}/regenerate", response_model=CollectionPlanResponse, status_code=201)
async def regenerate_collection_plan(
    plan_id: UUID,
    body: CollectionPlanRegenerateRequest,
    response: Response,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)],
    actor: Annotated[ActorContext, Depends(require_actor)],
    service: Annotated[CollectionPlanService, Depends(get_collection_plan_service)],
) -> CollectionPlanResponse:
    """以最新画像和 KBD 规则集重生成计划，并撤销旧计划制品。"""

    result = await service.regenerate(
        actor=actor,
        plan_id=str(plan_id),
        command=body,
        idempotency_key=idempotency_key,
    )
    response.headers["Idempotent-Replayed"] = "false" if result.created else "true"
    return CollectionPlanResponse.from_entities(result.entity, result.items)


@router.get("/{plan_id}", response_model=CollectionPlanResponse)
async def get_collection_plan(
    session_id: UUID,
    plan_id: UUID,
    actor: Annotated[ActorContext, Depends(require_actor)],
    service: Annotated[CollectionPlanService, Depends(get_collection_plan_service)],
) -> CollectionPlanResponse:
    """读取采集计划详情。"""

    result = await service.get(actor=actor, session_id=str(session_id), plan_id=str(plan_id))
    return CollectionPlanResponse.from_entities(result.entity, result.items)
