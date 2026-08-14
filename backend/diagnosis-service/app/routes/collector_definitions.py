"""安全 Collector Registry（采集器注册表）内部 API。"""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, Query, Response

from app.auth import ActorContext, require_actor
from app.dependencies import get_collector_definition_service
from app.schemas.collector_definition import (
    CollectorApprovalRequest,
    CollectorDefinitionResponse,
    CollectorDefinitionWrite,
)
from app.services.collector_definition_service import CollectorDefinitionService

router = APIRouter(prefix="/api/internal/collectors", tags=["collectors"])


@router.get("", response_model=list[CollectorDefinitionResponse])
async def list_collectors(
    actor: Annotated[ActorContext, Depends(require_actor)],
    service: Annotated[CollectorDefinitionService, Depends(get_collector_definition_service)],
    review_status: Annotated[Literal["draft", "approved", "rejected"] | None, Query()] = None,
    is_enabled: Annotated[bool | None, Query()] = None,
) -> list[CollectorDefinitionResponse]:
    """列出 Collector，可按审批状态和启用状态筛选。"""

    return await service.list(actor=actor, review_status=review_status, is_enabled=is_enabled)


@router.put("/{collector_id}", response_model=CollectorDefinitionResponse)
async def save_collector_draft(
    collector_id: str,
    body: CollectorDefinitionWrite,
    response: Response,
    actor: Annotated[ActorContext, Depends(require_actor)],
    service: Annotated[CollectorDefinitionService, Depends(get_collector_definition_service)],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> CollectorDefinitionResponse:
    """创建或更新 Collector 草稿。"""

    result = await service.save_draft(
        actor=actor,
        collector_id=collector_id,
        command=body,
        if_match=if_match,
    )
    response.headers["ETag"] = f'"{result.lock_version}"'
    return result


@router.post("/{collector_id}/review", response_model=CollectorDefinitionResponse)
async def review_collector(
    collector_id: str,
    body: CollectorApprovalRequest,
    response: Response,
    actor: Annotated[ActorContext, Depends(require_actor)],
    service: Annotated[CollectorDefinitionService, Depends(get_collector_definition_service)],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> CollectorDefinitionResponse:
    """批准或拒绝 Collector。"""

    result = await service.approve(
        actor=actor,
        collector_id=collector_id,
        command=body,
        if_match=if_match,
    )
    response.headers["ETag"] = f'"{result.lock_version}"'
    return result


@router.post("/{collector_id}/disable", response_model=CollectorDefinitionResponse)
async def disable_collector(
    collector_id: str,
    response: Response,
    actor: Annotated[ActorContext, Depends(require_actor)],
    service: Annotated[CollectorDefinitionService, Depends(get_collector_definition_service)],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> CollectorDefinitionResponse:
    """禁用 Collector。"""

    result = await service.disable(actor=actor, collector_id=collector_id, if_match=if_match)
    response.headers["ETag"] = f'"{result.lock_version}"'
    return result


@router.get("/{collector_id}", response_model=CollectorDefinitionResponse)
async def get_collector(
    collector_id: str,
    response: Response,
    actor: Annotated[ActorContext, Depends(require_actor)],
    service: Annotated[CollectorDefinitionService, Depends(get_collector_definition_service)],
) -> CollectorDefinitionResponse:
    """读取 Collector 定义。"""

    result = await service.get(actor=actor, collector_id=collector_id)
    response.headers["ETag"] = f'"{result.lock_version}"'
    return result
