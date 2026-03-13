"""
终端会话 HTTP API 路由
Task 37: SSH 代理与终端交互后端能力

提供：
- POST /api/terminal/sessions - 创建终端会话
- POST /api/terminal/sessions/{id}/close - 关闭终端会话
- GET /api/terminal/sessions/{id} - 获取会话信息
"""

from fastapi import APIRouter, Depends, HTTPException, Request

from ..models.terminal import (
    TerminalSessionClose,
    TerminalSessionCreate,
    TerminalSessionResponse,
)
from ..services.terminal import TerminalService

router = APIRouter(prefix="/api/terminal", tags=["terminal"])


def get_terminal_service(request: Request) -> TerminalService:
    """获取终端服务实例"""
    return request.app.state.terminal_service


@router.post("/sessions", response_model=TerminalSessionResponse)
async def create_session(
    body: TerminalSessionCreate,
    service: TerminalService = Depends(get_terminal_service),
):
    """
    创建终端会话

    建立 SSH 连接并返回会话 ID。
    """
    try:
        session_id, session_info = await service.create_session(
            host=body.host,
            port=body.port,
            username=body.username,
            auth_type=body.auth_type,
            password=body.password,
            private_key=body.private_key,
            passphrase=body.passphrase,
            client_id=body.client_id,
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
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"创建会话失败: {e}")


@router.post("/sessions/{session_id}/close", response_model=TerminalSessionClose)
async def close_session(
    session_id: str,
    service: TerminalService = Depends(get_terminal_service),
):
    """
    关闭终端会话

    关闭 SSH 连接并清理资源。
    """
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
    service: TerminalService = Depends(get_terminal_service),
):
    """
    获取会话信息

    返回会话的详细状态。
    """
    session_info = await service.get_session(session_id)

    if not session_info:
        raise HTTPException(status_code=404, detail="会话不存在")

    return session_info
