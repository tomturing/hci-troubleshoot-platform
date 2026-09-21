"""
终端会话 HTTP API 路由
Task 37: SSH 代理与终端交互后端能力
Task 42: 终端操作录制功能

提供：
- POST /api/terminal/sessions - 创建终端会话
- POST /api/terminal/sessions/{id}/close - 关闭终端会话
- GET /api/terminal/sessions/{id} - 获取会话信息
- POST /api/terminal/operations - 创建操作记录
- GET /api/terminal/operations - 查询操作记录
"""

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from shared.security.signature import sign_client_identity

from app.config import settings
from app.security.gateway_auth import require_user

from ..models.terminal import (
    OperationDirection,
    TerminalOperationCreate,
    TerminalOperationListResponse,
    TerminalOperationResponse,
    TerminalSessionClose,
    TerminalSessionCreate,
    TerminalSessionResponse,
)
from ..services.terminal import TerminalService

router = APIRouter(prefix="/api/terminal", tags=["terminal"])


def get_terminal_service(request: Request) -> TerminalService:
    """获取终端服务实例"""
    return request.app.state.terminal_service


async def verify_case_owner(case_id: str, client_id: str) -> None:
    """校验当前身份是否拥有该工单（调用 case-service，携带签名身份）。"""
    headers = sign_client_identity(client_id, settings.INTERNAL_API_TOKEN)
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{settings.CASE_SERVICE_URL}/api/cases/{case_id}", headers=headers)
    except httpx.RequestError:
        raise HTTPException(status_code=503, detail="Case Service unavailable")
    if resp.status_code != 200:
        # 404（不存在）或 403（非归属）统一按无权限处理，避免泄露存在性
        raise HTTPException(status_code=403, detail="无权访问该工单的终端数据")


@router.post("/sessions", response_model=TerminalSessionResponse)
async def create_session(
    body: TerminalSessionCreate,
    client_id: str = Depends(require_user),
    service: TerminalService = Depends(get_terminal_service),
):
    """
    创建终端会话

    建立 SSH 连接并返回会话 ID。client_id 一律以服务端身份 Cookie 为准。
    """
    try:
        # 若关联工单，先校验归属
        if body.case_id:
            await verify_case_owner(body.case_id, client_id)

        session_id, session_info = await service.create_session(
            host=body.host,
            port=body.port,
            username=body.username,
            auth_type=body.auth_type,
            password=body.password,
            private_key=body.private_key,
            passphrase=body.passphrase,
            client_id=client_id,
            case_id=body.case_id,
        )

        return TerminalSessionResponse(
            session_id=session_id,
            host=session_info.host,
            port=session_info.port,
            username=session_info.username,
            status="connected",
            message="SSH 连接成功",
        )

    except PermissionError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except ConnectionError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"创建会话失败: {e}")


@router.post("/sessions/{session_id}/close", response_model=TerminalSessionClose)
async def close_session(
    session_id: str,
    client_id: str = Depends(require_user),
    service: TerminalService = Depends(get_terminal_service),
):
    """
    关闭终端会话

    关闭 SSH 连接并清理资源。
    """
    session_info = await service.get_session(session_id)
    if not session_info:
        raise HTTPException(status_code=404, detail="会话不存在")

    if session_info.client_id != client_id:
        raise HTTPException(status_code=403, detail="无权限关闭该会话")

    success = await service.close_session(session_id)
    if not success:
        raise HTTPException(status_code=404, detail="会话不存在")

    return TerminalSessionClose(
        session_id=session_id,
        status="closed",
        message="会话已关闭",
    )


@router.get("/sessions/{session_id}")
async def get_session(
    session_id: str,
    client_id: str = Depends(require_user),
    service: TerminalService = Depends(get_terminal_service),
):
    """
    获取会话信息

    返回会话的详细状态。
    """
    session_info = await service.get_session(session_id)

    if not session_info:
        raise HTTPException(status_code=404, detail="会话不存在")

    if session_info.client_id != client_id:
        raise HTTPException(status_code=403, detail="无权限访问该会话")

    return session_info


# ============================================================
# 终端操作录制 API (Task 42)
# ============================================================


@router.post("/operations", response_model=TerminalOperationResponse)
async def create_operation(
    body: TerminalOperationCreate,
    client_id: str = Depends(require_user),
    service: TerminalService = Depends(get_terminal_service),
):
    """
    创建终端操作记录

    前端录制命令输入或输出回显后调用此接口写入数据库。
    """
    try:
        # 校验工单归属（若关联）
        if body.case_id:
            await verify_case_owner(body.case_id, client_id)

        operation = await service.create_operation(
            case_id=body.case_id,
            conversation_id=body.conversation_id,
            session_id=body.session_id,
            seq_number=body.seq_number,
            direction=body.direction,
            command=body.command,
            content=body.content,
            content_clean=body.content_clean,
            exit_code=body.exit_code,
            diagnostic_stage=body.diagnostic_stage,
        )
        return operation

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"创建操作记录失败: {e}")


@router.get("/operations", response_model=TerminalOperationListResponse)
async def list_operations(
    case_id: str = Query(..., description="工单 ID"),
    stage: str | None = Query(default=None, description="诊断阶段过滤"),
    search: str | None = Query(default=None, description="关键词搜索"),
    direction: OperationDirection | None = Query(default=None, description="方向过滤"),
    order: str = Query(default="asc", description="排序方向"),
    limit: int = Query(default=100, ge=1, le=1000, description="返回数量限制"),
    offset: int = Query(default=0, ge=0, description="偏移量"),
    client_id: str = Depends(require_user),
    service: TerminalService = Depends(get_terminal_service),
):
    """
    查询终端操作记录

    支持按工单、诊断阶段、关键词过滤，用于回放展示。强制校验工单归属。
    """
    try:
        await verify_case_owner(case_id, client_id)

        total, operations = await service.list_operations(
            case_id=case_id,
            stage=stage,
            search=search,
            direction=direction,
            order=order,
            limit=limit,
            offset=offset,
        )
        return TerminalOperationListResponse(total=total, operations=operations)

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"查询操作记录失败: {e}")
