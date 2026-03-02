"""
WebSocket Routes - 实时双向通信
"""

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Depends
import json
import httpx

from shared.utils.logger import get_logger
from ..services.session import SessionManager

router = APIRouter()
logger = get_logger("websocket-handler")

# 全局变量，在main.py中初始化
session_manager: SessionManager = None

def set_session_manager(sm: SessionManager):
    global session_manager
    session_manager = sm

@router.websocket("/ws/{client_id}")
async def websocket_endpoint(websocket: WebSocket, client_id: str):
    """WebSocket连接端点"""
    await websocket.accept()
    
    logger.info(
        event="websocket_connected",
        message=f"WebSocket connected",
        client_id=client_id
    )
    
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
                    message_type=message.get("type")
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
                        headers={"X-Client-ID": client_id}
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
            logger.info(
                event="websocket_disconnected",
                message=f"WebSocket disconnected",
                client_id=client_id
            )
            await session_manager.close_session(client_id)
        
        except Exception as e:
            logger.error(
                event="websocket_error",
                message="WebSocket error",
                client_id=client_id,
                error=str(e)
            )
            await session_manager.close_session(client_id)
