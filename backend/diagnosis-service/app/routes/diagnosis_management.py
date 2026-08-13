"""离线诊断管理工作台 API。"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from app.auth import ActorContext, require_actor
from app.dependencies import get_diagnosis_management_service
from app.schemas.diagnosis_management import (
    AssignDiagnosisSessionRequest,
    DiagnosisManagementActionResponse,
    DiagnosisManagementRecord,
    DiagnosisSessionManagementList,
    SecurityEventReviewRequest,
    TerminateDiagnosisSessionRequest,
)
from app.services.diagnosis_management_service import DiagnosisManagementService

router = APIRouter(tags=["diagnosis-management"])


@router.get("/api/internal/diagnosis-sessions", response_model=DiagnosisSessionManagementList)
async def list_managed_sessions(
    actor: Annotated[ActorContext, Depends(require_actor)],
    service: Annotated[DiagnosisManagementService, Depends(get_diagnosis_management_service)],
    query: str | None = Query(default=None, max_length=128),
    status: str | None = Query(default=None, max_length=32),
    assigned_to: str | None = Query(default=None, max_length=128),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
) -> DiagnosisSessionManagementList:
    """列出当前租户可管理的诊断会话。"""

    return DiagnosisSessionManagementList.model_validate(
        await service.list_sessions(
            actor=actor,
            query=query,
            status=status,
            assigned_to=assigned_to,
            offset=offset,
            limit=limit,
        )
    )


@router.post(
    "/api/internal/diagnosis-sessions/{session_id}/assign",
    response_model=DiagnosisManagementActionResponse,
)
async def assign_managed_session(
    session_id: UUID,
    body: AssignDiagnosisSessionRequest,
    actor: Annotated[ActorContext, Depends(require_actor)],
    service: Annotated[DiagnosisManagementService, Depends(get_diagnosis_management_service)],
) -> DiagnosisManagementActionResponse:
    """转派诊断会话。"""

    return DiagnosisManagementActionResponse.model_validate(
        await service.assign(actor=actor, session_id=str(session_id), assigned_to=body.assigned_to)
    )


@router.post(
    "/api/internal/diagnosis-sessions/{session_id}/terminate",
    response_model=DiagnosisManagementActionResponse,
)
async def terminate_managed_session(
    session_id: UUID,
    body: TerminateDiagnosisSessionRequest,
    actor: Annotated[ActorContext, Depends(require_actor)],
    service: Annotated[DiagnosisManagementService, Depends(get_diagnosis_management_service)],
) -> DiagnosisManagementActionResponse:
    """终止诊断会话。"""

    return DiagnosisManagementActionResponse.model_validate(
        await service.terminate(actor=actor, session_id=str(session_id), reason=body.reason)
    )


@router.post(
    "/api/internal/diagnosis-sessions/{session_id}/retry-processing",
    response_model=DiagnosisManagementActionResponse,
)
async def retry_managed_processing(
    session_id: UUID,
    actor: Annotated[ActorContext, Depends(require_actor)],
    service: Annotated[DiagnosisManagementService, Depends(get_diagnosis_management_service)],
) -> DiagnosisManagementActionResponse:
    """重试后台证据处理任务。"""

    return DiagnosisManagementActionResponse.model_validate(
        await service.retry_processing(actor=actor, session_id=str(session_id))
    )


@router.get(
    "/api/internal/diagnosis-sessions/report-reviews",
    response_model=list[DiagnosisManagementRecord],
)
async def list_report_reviews(
    actor: Annotated[ActorContext, Depends(require_actor)],
    service: Annotated[DiagnosisManagementService, Depends(get_diagnosis_management_service)],
) -> list[DiagnosisManagementRecord]:
    """列出报告审核队列。"""

    return [DiagnosisManagementRecord.model_validate(row) for row in await service.list_report_reviews(actor=actor)]


@router.get(
    "/api/internal/diagnosis-security/events",
    response_model=list[DiagnosisManagementRecord],
)
async def list_security_events(
    actor: Annotated[ActorContext, Depends(require_actor)],
    service: Annotated[DiagnosisManagementService, Depends(get_diagnosis_management_service)],
) -> list[DiagnosisManagementRecord]:
    """列出隔离区安全事件。"""

    return [DiagnosisManagementRecord.model_validate(row) for row in await service.list_security_events(actor=actor)]


@router.post(
    "/api/internal/diagnosis-security/events/{bundle_id}/review",
    response_model=DiagnosisManagementActionResponse,
)
async def review_security_event(
    bundle_id: UUID,
    body: SecurityEventReviewRequest,
    actor: Annotated[ActorContext, Depends(require_actor)],
    service: Annotated[DiagnosisManagementService, Depends(get_diagnosis_management_service)],
) -> DiagnosisManagementActionResponse:
    """确认或清除隔离区安全事件。"""

    return DiagnosisManagementActionResponse.model_validate(
        await service.review_security_event(
            actor=actor,
            bundle_id=str(bundle_id),
            action=body.action,
            note=body.note,
        )
    )


@router.get(
    "/api/internal/diagnosis-sessions/governance",
    response_model=list[DiagnosisManagementRecord],
)
async def list_diagnosis_governance(
    actor: Annotated[ActorContext, Depends(require_actor)],
    service: Annotated[DiagnosisManagementService, Depends(get_diagnosis_management_service)],
) -> list[DiagnosisManagementRecord]:
    """列出法务保全和删除任务。"""

    return [DiagnosisManagementRecord.model_validate(row) for row in await service.list_governance(actor=actor)]


@router.get(
    "/api/internal/diagnosis-sessions/audit",
    response_model=list[DiagnosisManagementRecord],
)
async def list_diagnosis_audit(
    actor: Annotated[ActorContext, Depends(require_actor)],
    service: Annotated[DiagnosisManagementService, Depends(get_diagnosis_management_service)],
    limit: int = Query(default=200, ge=1, le=1000),
) -> list[DiagnosisManagementRecord]:
    """列出全局诊断操作审计。"""

    return [DiagnosisManagementRecord.model_validate(row) for row in await service.list_audit(actor=actor, limit=limit)]
