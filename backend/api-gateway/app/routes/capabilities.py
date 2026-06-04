"""
智能体能力代理路由（Tools、Skills、Prompts） — API 网关代理层

负责将 /api/v1/tools、/api/v1/skills 和 /api/v1/prompts 的请求代理转发到 conversation-service。
"""

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from shared.observability.logger import get_logger

from app.config import settings

router = APIRouter(prefix="/api/v1", tags=["agent-capabilities-proxy"])
logger = get_logger("gateway-agent-capabilities")

# 上游 conversation-service 的地址
CONVERSATION_SERVICE_URL = settings.CONVERSATION_SERVICE_URL


async def proxy_request(
    method: str,
    path: str,
    payload: dict | None = None,
    params: dict | None = None,
    headers: dict | None = None,
):
    """泛向代理 HTTP 请求"""
    async with httpx.AsyncClient() as client:
        try:
            url = f"{CONVERSATION_SERVICE_URL}{path}"
            # 仅透传鉴权相关的 Header 以供上游校验
            req_headers = {}
            if headers and "authorization" in headers:
                req_headers["authorization"] = headers["authorization"]

            response = await client.request(method, url, json=payload, params=params, headers=req_headers)
            return response
        except httpx.RequestError as exc:
            logger.error(
                event="gateway_proxy_error",
                message=f"Error requesting upstream conversation-service: {exc}",
                url=exc.request.url if hasattr(exc, "request") else "",
            )
            raise HTTPException(status_code=503, detail="Upstream service unavailable")


@router.api_route("/tools", methods=["GET", "POST", "PUT", "DELETE"])
@router.api_route("/tools/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def proxy_tools(request: Request, path: str = ""):
    """透传工具管理请求"""
    method = request.method
    params = dict(request.query_params)
    payload = None
    if method in ["POST", "PUT"]:
        payload = await request.json()
    headers = {k.lower(): v for k, v in request.headers.items()}

    upstream_path = f"/api/v1/tools/{path}" if path else "/api/v1/tools"
    response = await proxy_request(method, upstream_path, payload=payload, params=params, headers=headers)

    # 兼容 FastAPI 新建资源成功返回的 210/201 等
    return JSONResponse(content=response.json(), status_code=response.status_code)


@router.api_route("/prompts", methods=["GET", "POST", "PUT", "DELETE"])
@router.api_route("/prompts/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def proxy_prompts(request: Request, path: str = ""):
    """透传 Prompt 管理请求"""
    method = request.method
    params = dict(request.query_params)
    payload = None
    if method in ["POST", "PUT"]:
        payload = await request.json()
    headers = {k.lower(): v for k, v in request.headers.items()}

    upstream_path = f"/api/v1/prompts/{path}" if path else "/api/v1/prompts"
    response = await proxy_request(method, upstream_path, payload=payload, params=params, headers=headers)

    return JSONResponse(content=response.json(), status_code=response.status_code)


@router.api_route("/skills", methods=["GET", "POST", "PUT", "DELETE"])
@router.api_route("/skills/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
async def proxy_skills(request: Request, path: str = ""):
    """透传技能管理请求"""
    method = request.method
    params = dict(request.query_params)
    payload = None
    if method in ["POST", "PUT"]:
        payload = await request.json()
    headers = {k.lower(): v for k, v in request.headers.items()}

    upstream_path = f"/api/v1/skills/{path}" if path else "/api/v1/skills"
    response = await proxy_request(method, upstream_path, payload=payload, params=params, headers=headers)

    return JSONResponse(content=response.json(), status_code=response.status_code)
