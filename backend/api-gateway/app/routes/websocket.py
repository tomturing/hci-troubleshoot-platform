"""
WebSocket Routes - 实时双向通信

安全审计 2026-08-19 修复：
1. 身份信任链：转发到 conversation-service 前剥离用户可伪造的身份头，
   改用网关 HMAC 签名（shared/security/signature.py）；归属校验由
   conversation-service 的 send_message 端点完成，403/404 经 WS 回传。
2. 协议修正：旧实现读 message["conversation_id"] 但 schema 只定义了
   case_id（实体混淆）。现 WebSocketMessage 以 conversation_id 为必填
   主字段，转发前经 Pydantic 校验（metadata 黑名单 + 大小限制）。
"""

import contextlib
import json
import re

import httpx
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import ValidationError
from shared.models.schemas import WebSocketMessage
from shared.observability.logger import get_logger
from shared.security.signature import CLIENT_ID_PATTERN, sign_client_identity

from app.config import settings

from ..services.session import SessionManager

router = APIRouter()
logger = get_logger("websocket-handler")

# 全局变量，在main.py中初始化
session_manager: SessionManager = None

CONVERSATION_SERVICE_URL = settings.CONVERSATION_SERVICE_URL


def set_session_manager(sm: SessionManager):
    global session_manager
    session_manager = sm


@router.websocket("/ws/{client_id}")
async def websocket_endpoint(websocket: WebSocket, client_id: str):
    """WebSocket连接端点"""
    await websocket.accept()

    # client_id 格式校验：拒绝畸形标识符（下游以其做归属比对）
    if not re.fullmatch(CLIENT_ID_PATTERN, client_id):
        await websocket.send_text(json.dumps({"error": "invalid_client_id", "message": "client_id 格式非法"}))
        await websocket.close(code=1008, reason="Policy Violation")
        return

    logger.info(event="websocket_connected", message="WebSocket connected", client_id=client_id)

    # 创建会话
    await session_manager.create_session(client_id, websocket)

    # 出口身份签名：归属校验凭据由网关签发，客户端无法伪造
    signed_identity = sign_client_identity(client_id, settings.INTERNAL_API_TOKEN)

    # 复用HTTP客户端
    async with httpx.AsyncClient(timeout=30.0) as http_client:
        try:
            while True:
                # 接收客户端消息
                data = await websocket.receive_text()
                try:
                    ws_message = WebSocketMessage.model_validate_json(data)
                except ValidationError as e:
                    errors = e.errors()[:5]
                    # 构造更有帮助的错误消息，包含缺失字段名
                    missing_fields = [err["loc"][-1] for err in errors if err.get("type") == "missing"]
                    error_msg = f"missing required field: {', '.join(missing_fields)}" if missing_fields else "invalid_schema"
                    logger.warning(
                        event="websocket_invalid_schema",
                        message="Invalid message schema",
                        client_id=client_id,
                        errors=errors,
                    )
                    await websocket.send_text(json.dumps({"error": error_msg, "details": errors}))
                    continue

                logger.info(
                    event="message_received",
                    message="Received message from client",
                    client_id=client_id,
                    message_type=ws_message.type,
                )

                # 转发到 Conversation Service
                try:
                    # 兼容原始协议：保留前端可能携带的 role/case_id 等附加字段，
                    # 但安全敏感字段（content/metadata）一律用校验后的值覆盖
                    try:
                        raw_fields = json.loads(data)
                    except json.JSONDecodeError:
                        raw_fields = {}
                    payload = raw_fields if isinstance(raw_fields, dict) else {}
                    payload["conversation_id"] = ws_message.conversation_id
                    payload["content"] = ws_message.content
                    payload["metadata"] = ws_message.metadata

                    # 使用流式请求
                    async with http_client.stream(
                        "POST",
                        f"{CONVERSATION_SERVICE_URL}/api/conversations/{ws_message.conversation_id}/message",
                        json=payload,
                        headers=dict(signed_identity),
                    ) as response:
                        if response.status_code != 200:
                            error_content = await response.read()
                            logger.warning(
                                event="websocket_upstream_rejected",
                                client_id=client_id,
                                status_code=response.status_code,
                                error=error_content.decode(errors="replace")[:200],
                            )
                            await websocket.send_text(json.dumps({
                                "error": "upstream_rejected",
                                "status_code": response.status_code,
                                "message": "上游服务拒绝请求（无权访问或会话不存在）",
                            }))
                            continue

                        # 流式返回AI响应
                        async for line in response.aiter_lines():
                            if line:
                                await websocket.send_text(line)

                except httpx.RequestError as exc:
                    logger.error(event="websocket_upstream_error", error_type=type(exc).__name__)
                    await websocket.send_text(json.dumps({"error": "service_unavailable", "message": "服务暂时不可用"}))

        except WebSocketDisconnect:
            logger.info(event="websocket_disconnected", message="WebSocket disconnected", client_id=client_id)
            await session_manager.close_session(client_id)

        except Exception as e:
            # 详细错误仅记录日志；对客户端只返回通用消息，避免信息泄漏
            logger.error(
                event="websocket_error",
                message="WebSocket error",
                client_id=client_id,
                error_type=type(e).__name__,
            )
            with contextlib.suppress(Exception):
                await websocket.send_text(json.dumps({"error": "internal_error", "message": "服务暂时不可用，请稍后重试"}))
            await session_manager.close_session(client_id)
