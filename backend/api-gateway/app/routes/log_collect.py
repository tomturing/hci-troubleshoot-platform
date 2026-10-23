"""日志收集（acli log_collect）控制面代理。

职责边界：
- 真实采集由客户 PC 上的 terminal_bridge 经 PTY 执行；分片数据面走
  ``/api/direct/log-collect-uploads`` 直连 case-service（32 MiB 超出网关 1 MiB 限制），
  因此不经本路由；
- 本路由只代理控制面：任务登记/状态机/分片会话/代传，全部落在 case-service。

鉴权：
- ``require_user`` 从服务端签发的身份 Cookie 取 client_id，再以 INTERNAL_API_TOKEN
  重签 ``X-Client-ID`` / ``X-Client-Signature`` 下发给 case-service；
- 客户端自报的身份头一律不转发；工单归属校验（BOLA）由 case-service 的
  ``verify_case_ownership`` 完成。

为什么用前缀通配：控制面端点随状态机持续演进（#1192→#1204 连续新增 uploads/relay），
逐条注册极易再次出现"后端有路由、网关 404"的缺口（本次修复的根因）。前缀内部仍由
case-service 的鉴权与归属校验兜底，网关不做业务判断，只保证身份重签与请求体上限。
"""

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from shared.observability.logger import get_logger
from shared.security.signature import sign_client_identity

from app.config import settings
from app.security.gateway_auth import require_user

router = APIRouter(prefix="/api/log-collect", tags=["log-collect-proxy"])
logger = get_logger("gateway-log-collect")

MAX_CONTROL_PLANE_BODY_BYTES = 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 30.0
# 分片合并是 case-service 内的流式落盘，20 GiB 远超声网默认控制面超时。
MERGE_TIMEOUT_SECONDS = 600.0
FORWARDED_REQUEST_HEADERS = (
    "content-type",
    "accept",
    "traceparent",
    "tracestate",
    "idempotency-key",
)
FORWARDED_RESPONSE_HEADERS = ("content-type", "cache-control", "etag")


def _upstream_timeout(path: str) -> float:
    """按端点选择超时：仅分片合并需要长超时，其余保持快速失败。"""
    return MERGE_TIMEOUT_SECONDS if path.endswith("/uploads/complete") else DEFAULT_TIMEOUT_SECONDS


def _signed_headers(request: Request, client_id: str) -> dict[str, str]:
    """构造下游请求头：网关身份重签 + 仅转发白名单内的协议头。"""
    headers = sign_client_identity(client_id, settings.INTERNAL_API_TOKEN)
    for name in FORWARDED_REQUEST_HEADERS:
        value = request.headers.get(name)
        if value:
            headers[name] = value
    return headers


def _ensure_body_size(request: Request, body: bytes) -> None:
    """控制面请求体上限 1 MiB；数据面分片不得经此路径。"""
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            declared = int(content_length)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Content-Length 格式不合法") from exc
        if declared > MAX_CONTROL_PLANE_BODY_BYTES:
            raise HTTPException(status_code=413, detail="日志收集控制面请求体不能超过 1 MiB")
    if len(body) > MAX_CONTROL_PLANE_BODY_BYTES:
        raise HTTPException(status_code=413, detail="日志收集控制面请求体不能超过 1 MiB")


@router.api_route("/tasks{path:path}", methods=["GET", "POST", "PATCH", "DELETE"])
async def proxy_log_collect(
    request: Request,
    path: str = "",
    client_id: str = Depends(require_user),
) -> Response:
    """把控制面请求按客户身份重签后转发到 case-service，并原样透传响应。

    响应（含 RFC 7807 错误体与状态码）不做改写：业务错误码目录属于 case-service，
    网关若二次包装会让前端拿到与文档不一致的结构。
    """
    body = await request.body()
    _ensure_body_size(request, body)

    url = f"{settings.CASE_SERVICE_URL.rstrip('/')}/api/log-collect/tasks{path}"
    try:
        async with httpx.AsyncClient(timeout=_upstream_timeout(path)) as client:
            upstream = await client.request(
                request.method,
                url,
                content=body or None,
                headers=_signed_headers(request, client_id),
                params=dict(request.query_params),
            )
    except httpx.RequestError as exc:
        logger.error(
            event="log_collect_proxy_error",
            message=str(exc),
            path=path,
            method=request.method,
        )
        raise HTTPException(status_code=503, detail="Case Service unavailable") from exc

    response_headers = {name: upstream.headers[name] for name in FORWARDED_RESPONSE_HEADERS if name in upstream.headers}
    return Response(content=upstream.content, status_code=upstream.status_code, headers=response_headers)
