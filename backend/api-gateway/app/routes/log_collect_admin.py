"""日志收集「故障场景」字典的管理端代理。

职责边界：
- 本路由只做两件事：**管理端鉴权**（require_admin，JWT 验签）与**内部令牌重签**，
  业务校验（字段合法性、value 不可改、停用语义）全部在 case-service；
- 为什么不复用 ``/api/log-collect`` 的前缀代理：那条代理把客户身份（X-Client-ID）
  重签给下游，而字典维护是平台运营动作、不属于任何客户，两条身份链路不能混用。

审计与追溯：管理端前端在 ``X-Admin-User`` 里带上操作者，原样透传给 case-service
落 ``updated_by`` 与 operation_audit；缺失时下游记 NULL，不编造身份。
"""

from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from shared.observability.logger import get_logger

from app.config import settings
from app.security.gateway_auth import require_admin

router = APIRouter(
    prefix="/api/v1/log-collect-scenarios",
    tags=["log-collect-scenarios-admin"],
    dependencies=[Depends(require_admin)],
)
logger = get_logger("gateway-log-collect-scenarios")

TIMEOUT_SECONDS = 15.0
UPSTREAM_PREFIX = "/internal/log-collect/scenarios"


def _internal_headers(request: Request) -> dict[str, str]:
    """内部令牌 + 操作者透传（操作者仅用于审计留痕，不参与鉴权）。

    操作者来源优先级：前端显式 `X-Admin-User` → 网关验签后的 JWT ``sub``。
    后者是服务端可信身份，前端没传时用它兜底，避免 `updated_by` 全部落空；
    两者都没有（例如共享令牌调用的脚本）时留空，不编造身份。
    """
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


async def _request_upstream(
    method: str,
    path: str,
    request: Request,
    payload: Any | None = None,
) -> Response:
    url = f"{settings.CASE_SERVICE_URL.rstrip('/')}{UPSTREAM_PREFIX}{path}"
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
            upstream = await client.request(method, url, json=payload, headers=_internal_headers(request))
    except httpx.RequestError as exc:
        logger.error(event="log_collect_scenario_admin_proxy_error", message=str(exc), path=path, method=method)
        raise HTTPException(status_code=503, detail="Case Service unavailable") from exc

    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers={"content-type": upstream.headers.get("content-type", "application/json")},
    )


@router.get("")
async def list_scenarios(request: Request) -> Response:
    """管理端列表（含停用项）。"""
    return await _request_upstream("GET", "", request)


@router.post("", status_code=201)
async def create_scenario(request: Request) -> Response:
    payload = await request.json()
    return await _request_upstream("POST", "", request, payload)


@router.put("/{value}")
async def update_scenario(value: str, request: Request) -> Response:
    payload = await request.json()
    return await _request_upstream("PUT", f"/{value}", request, payload)
