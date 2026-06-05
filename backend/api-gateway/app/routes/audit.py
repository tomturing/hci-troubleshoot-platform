"""
Audit Logs Routes - API Gateway Proxy

代理转发到 conversation-service 的审计日志接口。
"""

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from shared.observability.logger import get_logger

from app.config import settings

router = APIRouter(prefix="/api/audit-logs", tags=["audit"])

CONVERSATION_SERVICE_URL = settings.CONVERSATION_SERVICE_URL
logger = get_logger("gateway-audit")


async def proxy_request(
    method: str,
    path: str,
    payload: dict | None = None,
    params: dict | None = None,
):
    """代理请求到 conversation-service"""
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            url = f"{CONVERSATION_SERVICE_URL}/api/v1/audit-logs{path}"
            response = await client.request(
                method,
                url,
                json=payload,
                params=params,
            )
            return response
        except httpx.RequestError as exc:
            logger.error(
                event="proxy_request_error",
                message=f"Error requesting {exc.request.url!r}",
                path=path,
            )
            raise HTTPException(status_code=503, detail="Conversation Service unavailable")


@router.get("")
async def list_audit_logs(request: Request):
    """查询工具调用审计日志"""
    params = dict(request.query_params)
    response = await proxy_request("GET", "", params=params)
    return JSONResponse(content=response.json(), status_code=response.status_code)


@router.get("/prompts")
async def list_prompt_audit_logs(request: Request):
    """查询 Prompt 审计日志"""
    params = dict(request.query_params)
    response = await proxy_request("GET", "/prompts", params=params)
    return JSONResponse(content=response.json(), status_code=response.status_code)
