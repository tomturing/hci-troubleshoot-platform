"""诊断会话 API。"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Response

from app.auth import ActorContext, require_actor
from app.dependencies import get_session_service
from app.schemas.diagnosis_session import DiagnosisSessionCreate, DiagnosisSessionResponse
from app.services.diagnosis_session_service import DiagnosisSessionService

router = APIRouter(prefix="/api/diagnosis-sessions", tags=["diagnosis-sessions"])


@router.post("", response_model=DiagnosisSessionResponse, status_code=201)
async def create_diagnosis_session(
    body: DiagnosisSessionCreate,
    response: Response,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)],
    actor: Annotated[ActorContext, Depends(require_actor)],
    service: Annotated[DiagnosisSessionService, Depends(get_session_service)],
) -> DiagnosisSessionResponse:
    """创建诊断会话。"""

    result = await service.create(actor=actor, command=body, idempotency_key=idempotency_key)
    response.headers["Idempotent-Replayed"] = "false" if result.created else "true"
    response.headers["ETag"] = f'"{result.entity.version}"'
    return DiagnosisSessionResponse.from_entity(result.entity)


@router.get("/{session_id}", response_model=DiagnosisSessionResponse)
async def get_diagnosis_session(
    session_id: UUID,
    response: Response,
    actor: Annotated[ActorContext, Depends(require_actor)],
    service: Annotated[DiagnosisSessionService, Depends(get_session_service)],
) -> DiagnosisSessionResponse:
    """读取本租户诊断会话。"""

    entity = await service.get(actor=actor, session_id=session_id)
    response.headers["ETag"] = f'"{entity.version}"'
    return DiagnosisSessionResponse.from_entity(entity)


@router.get("/by-case/{case_id}/workspace")
async def resume_diagnosis_workspace(
    case_id: str,
    actor: Annotated[ActorContext, Depends(require_actor)],
    service: Annotated[DiagnosisSessionService, Depends(get_session_service)],
) -> dict:
    """按工单恢复离线诊断工作区，不返回上传令牌等内部凭据。"""

    workspace = await service.resume_workspace(actor=actor, case_id=case_id)
    return {
        **workspace,
        "session": DiagnosisSessionResponse.from_entity(workspace["session"]),
    }
