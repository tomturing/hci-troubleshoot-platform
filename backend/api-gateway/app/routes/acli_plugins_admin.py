"""acli 插件管理端网关代理：管理 JWT 验签 → 内部令牌重签 → case-service。

与 ``capabilities.py`` 的泛代理不同，本族路由整族 ``require_admin``，且**不透传**
管理员 JWT 给下游；配置保存、幂等发布与状态查询的业务语义全在 case-service，
网关只负责鉴权边界、超时和错误转换。页面请求体里没有 SSH 凭据与目标节点。
"""

from __future__ import annotations

from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from shared.observability.logger import get_logger

from app.config import settings
from app.security.gateway_auth import require_admin

router = APIRouter(
    prefix="/api/v1/acli-plugins",
    tags=["acli-plugins-admin"],
    dependencies=[Depends(require_admin)],
)
logger = get_logger("gateway-acli-plugins")

UPSTREAM_PREFIX = "/internal/acli-plugins"
# 保存配置会同步写 3 张表并等待上游校验，比纯读略长；下发本身异步，不在这里等。
TIMEOUT_SECONDS = 30.0


def _internal_headers(request: Request) -> dict[str, str]:
    token = settings.INTERNAL_API_TOKEN
    if not token or not str(token).strip():
        raise HTTPException(status_code=500, detail="INTERNAL_API_TOKEN 未配置，无法调用内部服务")
    headers = {"Authorization": f"Bearer {str(token).strip()}"}
    admin_user = request.headers.get("x-admin-user")
    if not admin_user:
        actor = getattr(request.state, "auth", None)
        admin_user = getattr(actor, "user_id", None)
    if admin_user:
        headers["X-Admin-User"] = str(admin_user)
    return headers


async def _proxy(
    method: str,
    path: str,
    request: Request,
    payload: Any | None = None,
    *,
    query: str | None = None,
) -> Response:
    url = f"{settings.CASE_SERVICE_URL.rstrip('/')}{UPSTREAM_PREFIX}{path}"
    if query:
        url = f"{url}?{query}"
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
            upstream = await client.request(method, url, json=payload, headers=_internal_headers(request))
    except httpx.RequestError as exc:
        logger.error(event="acli_plugins_admin_proxy_error", message=str(exc), path=path, method=method)
        raise HTTPException(status_code=503, detail="Case Service unavailable") from exc
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers={"content-type": upstream.headers.get("content-type", "application/json")},
    )


@router.get("/templates")
async def get_templates(request: Request) -> Response:
    return await _proxy("GET", "/templates", request)


@router.get("/environments")
async def list_environments(request: Request) -> Response:
    return await _proxy("GET", "/environments", request)


@router.get("/plugins")
async def list_plugins(request: Request) -> Response:
    return await _proxy("GET", "/plugins", request)


@router.post("/plugins")
async def publish_plugin(request: Request) -> Response:
    payload = await request.json()
    return await _proxy("POST", "/plugins", request, payload)


@router.get("/plugins/{plugin_name}/deployments")
async def list_deployments(plugin_name: str, request: Request) -> Response:
    query = request.url.query
    return await _proxy("GET", f"/plugins/{plugin_name}/deployments", request, query=query or None)


@router.get("/deployments/{deploy_id}")
async def get_deployment(deploy_id: str, request: Request) -> Response:
    return await _proxy("GET", f"/deployments/{deploy_id}", request)
