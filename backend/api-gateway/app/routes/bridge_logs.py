"""
api-gateway 代理路由：/api/bridge-logs -> conversation-service

复用 capabilities.py 的选择性行政转发模式 + conversations.py 的占位符兜底。
customer 前端无真实 session，靠网关注入占位符 token（对齐 exec-result 路由）。
"""

import logging

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.config import settings

logger = logging.getLogger("bridge-logs-proxy")
router = APIRouter(prefix="/api/bridge-logs", tags=["bridge-logs"])
CONVERSATION_SERVICE_URL = settings.CONVERSATION_SERVICE_URL


@router.post("")
async def proxy_bridge_logs(request: Request):
    """代理 bridge-logs 回采请求到 conversation-service。

    鉴权策略（对齐 exec-result 路由）：
      - 有 Authorization 头时透传
      - 无 Authorization 时注入占位符 token（customer 前端经网关兜底）
    """
    payload = await request.json()
    headers = {}
    auth = request.headers.get("Authorization")
    if auth:
        headers["Authorization"] = auth
    else:
        # 兜底注入占位符 token，对齐 conversations.py exec-result 路由
        headers["Authorization"] = "Bearer client-session-placeholder-token"

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            response = await client.post(
                f"{CONVERSATION_SERVICE_URL}/api/bridge-logs",
                json=payload,
                headers=headers,
            )
            return JSONResponse(
                content=response.json(),
                status_code=response.status_code,
            )
        except httpx.RequestError as exc:
            logger.error(
                f"gateway_bridge_logs_proxy_error: {exc}",
                extra={"event": "gateway_bridge_logs_proxy_error", "error": str(exc)},
            )
            from fastapi import HTTPException
            raise HTTPException(
                status_code=503,
                detail="Conversation service unavailable",
            )
