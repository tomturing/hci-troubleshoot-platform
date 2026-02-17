"""
WebSocket Routes - 实时双向通信
"""

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Depends
import json
import httpx

import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../..')))

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
    
    try:
        while True:
            # 接收客户端消息
            data = await websocket.receive_text()
            message = json.loads(data)
            
            logger.info(
                event="message_received",
                message="Received message from client",
                client_id=client_id,
                message_type=message.get("type")
            )
            
            # 转发到Conversation Service
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    "http://conversation-service:8002/api/conversations/message",
                    json=message,
                    headers={"X-Client-ID": client_id}
                )
                
                if response.status_code == 200:
                    # 流式返回AI响应
                    async for line in response.aiter_lines():
                        if line:
                            await websocket.send_text(line)
    
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
            error=e
        )
        await session_manager.close_session(client_id)
