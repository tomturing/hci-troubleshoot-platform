"""
WebSocket Routes - 实时双向通信

包含：
- /ws/{client_id} - AI 对话 WebSocket
- /ws/terminal/{session_id} - 终端交互 WebSocket (Task 37)
"""

import json

import httpx
from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from shared.utils.logger import get_logger

from ..models.terminal import TerminalWSMessage
from ..services.session import SessionManager
from ..services.terminal import TerminalService

router = APIRouter()
logger = get_logger("websocket-handler")

# 全局变量，在main.py中初始化
session_manager: SessionManager = None
terminal_service: TerminalService = None


def set_session_manager(sm: SessionManager):
    global session_manager
    session_manager = sm


def set_terminal_service(ts: TerminalService):
    """设置终端服务实例"""
    global terminal_service
    terminal_service = ts


@router.websocket("/ws/{client_id}")
async def websocket_endpoint(websocket: WebSocket, client_id: str):
    """WebSocket连接端点"""
    await websocket.accept()

    logger.info(event="websocket_connected", message="WebSocket connected", client_id=client_id)

    # 创建会话
    await session_manager.create_session(client_id, websocket)

    # 复用HTTP客户端
    async with httpx.AsyncClient() as http_client:
        try:
            while True:
                # 接收客户端消息
                data = await websocket.receive_text()
                try:
                    message = json.loads(data)
                except json.JSONDecodeError:
                    logger.warning(f"Invalid JSON received from {client_id}")
                    continue

                logger.info(
                    event="message_received",
                    message="Received message from client",
                    client_id=client_id,
                    message_type=message.get("type"),
                )

                # 转发到Conversation Service
                try:
                    conversation_id = message.get("conversation_id")
                    if not conversation_id:
                        await websocket.send_text(json.dumps({"error": "Missing conversation_id"}))
                        continue

                    # 使用流式请求
                    async with http_client.stream(
                        "POST",
                        f"http://conversation-service:8002/api/conversations/{conversation_id}/message",
                        json=message,
                        headers={"X-Client-ID": client_id},
                    ) as response:
                        if response.status_code != 200:
                            error_content = await response.read()
                            logger.error(f"Upstream error: {response.status_code} - {error_content}")
                            await websocket.send_text(json.dumps({"error": "Upstream service error"}))
                            continue

                        # 流式返回AI响应
                        async for line in response.aiter_lines():
                            if line:
                                await websocket.send_text(line)

                except httpx.RequestError as exc:
                    logger.error(f"Upstream connection error: {exc}")
                    await websocket.send_text(json.dumps({"error": "Service unavailable"}))

        except WebSocketDisconnect:
            logger.info(event="websocket_disconnected", message="WebSocket disconnected", client_id=client_id)
            await session_manager.close_session(client_id)

        except Exception as e:
            logger.error(event="websocket_error", message="WebSocket error", client_id=client_id, error=str(e))
            await session_manager.close_session(client_id)


@router.websocket("/ws/terminal/{session_id}")
async def terminal_websocket_endpoint(
    websocket: WebSocket,
    session_id: str,
    client_id: str | None = Query(default=None),
):
    """
    终端交互 WebSocket 端点 (Task 37)

    协议：
    - 客户端 -> 服务端: {"type":"stdin","data":"ls -la\\n"}
    - 服务端 -> 客户端: {"type":"stdout","data":"..."}
    - 服务端 -> 客户端: {"type":"stderr","data":"..."}
    - 服务端 -> 客户端: {"type":"status","state":"connected|disconnected|error","message":"..."}
    """
    await websocket.accept()

    if not client_id:
        await websocket.send_text(json.dumps({"type": "error", "data": "缺少 client_id，无法建立终端连接"}))
        await websocket.close(code=1008)
        return

    # 验证会话存在
    session_info = await terminal_service.validate_session_owner(session_id, client_id)
    if not session_info:
        await websocket.send_text(json.dumps({"type": "error", "data": "会话不存在、已过期或无访问权限"}))
        await websocket.close(code=1008)
        return

    await terminal_service.ssh_manager.add_websocket(session_id, websocket)

    logger.info(
        event="terminal_websocket_connected",
        message="终端 WebSocket 已连接",
        session_id=session_id,
        host=session_info.host,
        username=session_info.username,
    )

    try:
        while True:
            # 接收客户端消息
            data = await websocket.receive_text()

            try:
                message = TerminalWSMessage.model_validate_json(data)
            except Exception as e:
                logger.warning(
                    event="terminal_invalid_message",
                    message=f"无效的消息格式: {e}",
                    session_id=session_id,
                    raw_data=data[:100],
                )
                continue

            logger.debug(
                event="terminal_message_received",
                message="收到终端消息",
                session_id=session_id,
                message_type=message.type,
            )

            # 处理消息
            await terminal_service.handle_websocket_message(
                session_id=session_id,
                message=message,
                websocket=websocket,
            )

    except WebSocketDisconnect:
        logger.info(
            event="terminal_websocket_disconnected",
            message="终端 WebSocket 断开",
            session_id=session_id,
        )
        await terminal_service.ssh_manager.remove_websocket(session_id)

    except Exception as e:
        logger.error(
            event="terminal_websocket_error",
            message="终端 WebSocket 错误",
            session_id=session_id,
            error=str(e),
        )
        await terminal_service.ssh_manager.remove_websocket(session_id)
