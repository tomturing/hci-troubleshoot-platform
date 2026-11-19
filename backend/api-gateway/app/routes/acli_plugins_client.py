"""acli 插件阶段二网关代理：取票（客户身份）+ manifest / 包下载（票据透传）。

与 ``acli_plugins_admin.py``（管理端 JWT 族）分文件的理由：本族里 manifest 与包
下载的调用方是 **terminal_bridge**，它既没有管理 JWT 也没有客户 Cookie——凭据只有
短期只读票据。网关因此**只透传、不解析、不签发**票据（收口设计 §3.3 票据模型），
校验一律在 case-service 完成，任一异常 401。

三条路由逐条枚举（AGENTS.md 硬约束：网关新增后端路由必须显式补代理）：

* ``POST /api/acli-plugins/ticket``：客户身份取票。``require_user`` 从服务端签发
  的身份 Cookie 取 client_id，再以 INTERNAL_API_TOKEN 重签身份头下发——浏览器自报
  的身份一律不转发；
* ``GET /api/acli-plugins/manifest``：票据换期望态（小 JSON）；
* ``GET /api/acli-plugins/package/{plugin_name}``：票据换包（几十 MB，流式转发，
  不在网关内存里整包落地）。
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from shared.observability.logger import get_logger
from shared.security.signature import sign_client_identity

from app.config import settings
from app.security.gateway_auth import require_user

router = APIRouter(prefix="/api/acli-plugins", tags=["acli-plugins-sync"])
logger = get_logger("gateway-acli-plugins-sync")

# 票据鉴权端点在 case-service 挂在 /internal/acli-plugins 下（与管理员同表不同族）。
INTERNAL_PREFIX = "/internal/acli-plugins"
CLIENT_PREFIX = "/api/acli-plugins"
CONTROL_TIMEOUT_SECONDS = 30.0
# 包下载：单包 20–31 MB，客户侧上行常见 0.3–4 MB/s，读取窗口按下载链路放宽。
PACKAGE_TIMEOUT_SECONDS = 600.0
FORWARDED_TICKET_HEADERS = ("x-plugin-ticket",)
FORWARDED_RESPONSE_HEADERS = ("content-type", "content-length", "etag", "cache-control")


def _base() -> str:
    return settings.CASE_SERVICE_URL.rstrip("/")


def _ticket_headers(request: Request, client_id: str) -> dict[str, str]:
    headers = sign_client_identity(client_id, settings.INTERNAL_API_TOKEN)
    for name in ("content-type", "accept", "traceparent", "tracestate", "x-trace-id"):
        value = request.headers.get(name)
        if value:
            headers[name] = value
    return headers


def _ticket_passthrough_headers(request: Request) -> dict[str, str]:
    """只带票据与协议头，不带任何身份/管理员凭据——桥本来就不该拿到它们。"""
    headers = {"accept": request.headers.get("accept") or "application/json"}
    for name in FORWARDED_TICKET_HEADERS:
        value = request.headers.get(name)
        if value:
            headers[name] = value
    for name in ("traceparent", "tracestate", "x-trace-id"):
        value = request.headers.get(name)
        if value:
            headers[name] = value
    return headers


@router.post("/ticket")
async def issue_ticket(
    request: Request,
    client_id: str = Depends(require_user),
) -> Response:
    """客户面取票：工单归属与存在性由 case-service 判定，网关不解析 body。"""
    body = await request.body()
    url = f"{_base()}{CLIENT_PREFIX}/ticket"
    try:
        async with httpx.AsyncClient(timeout=CONTROL_TIMEOUT_SECONDS) as client:
            upstream = await client.post(
                url,
                content=body or None,
                headers=_ticket_headers(request, client_id),
            )
    except httpx.RequestError as exc:
        logger.error(event="acli_plugin_ticket_proxy_error", message=str(exc))
        raise HTTPException(status_code=503, detail="Case Service unavailable") from exc
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers={"content-type": upstream.headers.get("content-type", "application/json")},
    )


async def _forward_ticketed(method: str, path: str, request: Request) -> Response:
    """票据族通用转发：查询串原样带上（case_id / spec_hash 参与票据绑定）。"""
    url = f"{_base()}{INTERNAL_PREFIX}{path}"
    params: dict[str, Any] = dict(request.query_params)
    try:
        async with httpx.AsyncClient(timeout=CONTROL_TIMEOUT_SECONDS) as client:
            upstream = await client.request(
                method,
                url,
                params=params,
                headers=_ticket_passthrough_headers(request),
            )
    except httpx.RequestError as exc:
        logger.error(event="acli_plugin_manifest_proxy_error", message=str(exc), path=path)
        raise HTTPException(status_code=503, detail="Case Service unavailable") from exc
    headers = {name: upstream.headers[name] for name in FORWARDED_RESPONSE_HEADERS if name in upstream.headers}
    return Response(content=upstream.content, status_code=upstream.status_code, headers=headers)


@router.get("/manifest")
async def get_manifest(request: Request) -> Response:
    """插件期望态清单：小 JSON，整读整回。"""
    return await _forward_ticketed("GET", "/manifest", request)


@router.get("/package/{plugin_name}")
async def get_package(plugin_name: str, request: Request) -> Response:
    """插件包下载：流式转发，避免在网关内存里整包缓冲。

    上游 404（未配置存储 / 包不存在）也原样透传状态码与错误体：桥据此回退到
    manifest 内联文件收敛，而不是把一个截断的空响应当成下载成功。
    """
    url = f"{_base()}{INTERNAL_PREFIX}/package/{quote(plugin_name, safe='')}"
    timeout = httpx.Timeout(PACKAGE_TIMEOUT_SECONDS, connect=10.0)
    client = httpx.AsyncClient(timeout=timeout)
    try:
        upstream = await client.send(
            client.build_request(
                "GET",
                url,
                params=dict(request.query_params),
                headers=_ticket_passthrough_headers(request),
            ),
            stream=True,
        )
    except httpx.RequestError as exc:
        await client.aclose()
        logger.error(event="acli_plugin_package_proxy_error", message=str(exc), plugin_name=plugin_name)
        raise HTTPException(status_code=503, detail="Case Service unavailable") from exc
    if upstream.status_code >= 400:
        content = await upstream.aread()
        await upstream.aclose()
        await client.aclose()
        return Response(content=content, status_code=upstream.status_code, headers={"content-type": upstream.headers.get("content-type", "application/json")})

    async def body_stream():
        try:
            async for chunk in upstream.aiter_bytes():
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    headers = {name: upstream.headers[name] for name in FORWARDED_RESPONSE_HEADERS if name in upstream.headers}
    return StreamingResponse(
        body_stream(),
        status_code=upstream.status_code,
        headers=headers,
        media_type=upstream.headers.get("content-type", "application/octet-stream"),
    )
