"""
api-gateway 代理路由：/api/bridge-logs -> conversation-service

安全审计 L2 修复：移除 MVP 阶段的占位符 token 兜底（原 customer 前端经网关
注入固定占位符 token 绕过下游伪鉴权）。现对齐 conversations.py：
  - 网关 require_user 强制读取服务端签发的身份 Cookie，取得可信 client_id；
  - 出口前剥离用户可伪造的 X-Client-ID / X-Client-Signature，改用共享密钥
    HMAC 重签注入（见 shared/security/signature.py）；
  - 下游 conversation-service 据此做工单/会话归属校验（防 IDOR）。
"""

import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from shared.security.signature import sign_client_identity

from app.config import settings
from app.security.gateway_auth import require_user

logger = logging.getLogger("bridge-logs-proxy")
router = APIRouter(prefix="/api/bridge-logs", tags=["bridge-logs"])
CONVERSATION_SERVICE_URL = settings.CONVERSATION_SERVICE_URL

# 用户可伪造的身份头变体（出口前一律剥离）
_IDENTITY_HEADERS = ("x-client-id", "x-client-signature")


def _signed_outbound_headers(passthrough: dict, client_id: str | None) -> dict:
    """剥离用户自报身份头，注入网关 HMAC 签名（client_id 来自服务端 Cookie）。"""
    merged = {k: v for k, v in (passthrough or {}).items() if k.lower() not in _IDENTITY_HEADERS}
    if client_id:
        merged.update(sign_client_identity(client_id, settings.INTERNAL_API_TOKEN))
    return merged


@router.post("")
async def proxy_bridge_logs(request: Request, client_id: str = Depends(require_user)):
    """代理 bridge-logs 批量回采请求到 conversation-service。

    鉴权策略（L2 收紧，对齐 conversations.py）：
      - require_user 强制服务端签发的身份 Cookie，取得可信 client_id；
      - 出口注入 HMAC 签名 X-Client-ID，由下游做工单归属校验；
      - 透传 Authorization 仅为兼容内部直连，不再注入占位符 token。
    """
    payload = await request.json()
    headers = {}
    if auth := request.headers.get("Authorization"):
        headers["Authorization"] = auth
    if traceparent := request.headers.get("traceparent"):
        headers["traceparent"] = traceparent
    headers = _signed_outbound_headers(headers, client_id)

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
            raise HTTPException(
                status_code=503,
                detail="Conversation service unavailable",
            )


@router.post("/upload")
async def proxy_bridge_logs_upload(request: Request, client_id: str = Depends(require_user)):
    """代理本地日志手动上传补采请求到 conversation-service。

    鉴权策略与批量回采一致：require_user + 网关签名 X-Client-ID，
    透传 Authorization 仅为兼容，不再注入占位符 token。
    """
    payload = await request.json()
    headers = {}
    if auth := request.headers.get("Authorization"):
        headers["Authorization"] = auth
    if traceparent := request.headers.get("traceparent"):
        headers["traceparent"] = traceparent
    headers = _signed_outbound_headers(headers, client_id)

    async with httpx.AsyncClient(timeout=120.0) as client:
        try:
            response = await client.post(
                f"{CONVERSATION_SERVICE_URL}/api/bridge-logs/upload",
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
            raise HTTPException(
                status_code=503,
                detail="Conversation service unavailable",
            )
