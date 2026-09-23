"""认证服务反向代理（阶段1.x）。

将 /api/auth/*（登录、登出等）原样转发到 auth-service。登录是公开端点，
不要求网关身份；网关仅做协议级透传（请求体 / 安全头 / 链路追踪），
不向下游重签内部身份（auth-service 自身为认证边界）。

JWKS 公钥端点（/.well-known/jwks.json）由网关 JwtVerifier 直接拉取，不经此路由。
"""

import uuid

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from shared.observability.logger import get_logger

from app.config import settings

router = APIRouter(tags=["auth-proxy"])
logger = get_logger("gateway-auth-proxy")

# 透传到 auth-service 的请求头（含 Authorization，便于登录响应与后续鉴权链路一致）
FORWARDED_REQUEST_HEADERS = (
    "content-type",
    "accept",
    "authorization",
    "idempotency-key",
    "traceparent",
    "tracestate",
    "x-trace-id",
)
FORWARDED_RESPONSE_HEADERS = (
    "content-type",
    "cache-control",
    "set-cookie",
)


def _error_response(request: Request, *, status_code: int, code: str, message: str) -> JSONResponse:
    trace_id = request.headers.get("X-Trace-Id") or uuid.uuid4().hex
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message": message,
                "trace_id": trace_id,
                "retryable": status_code >= 500,
                "details": {},
            }
        },
    )


async def _proxy_auth_request(request: Request) -> Response:
    """将认证请求转发到 auth-service 并返回上游响应。"""
    headers: dict[str, str] = {}
    for name in FORWARDED_REQUEST_HEADERS:
        if value := request.headers.get(name):
            headers[name] = value
    # 携带调用链，便于跨服务追踪
    trace_id = request.headers.get("X-Trace-Id") or request.headers.get("traceparent") or uuid.uuid4().hex
    headers.setdefault("X-Trace-Id", trace_id)

    body = await request.body()
    upstream_url = f"{settings.AUTH_SERVICE_URL.rstrip('/')}{request.url.path}"
    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as client:
            upstream = await client.request(
                request.method,
                upstream_url,
                content=body or None,
                params=request.query_params,
                headers=headers,
            )
    except httpx.RequestError as exc:
        logger.error(
            event="gateway_auth_proxy_error",
            message="auth-service 请求失败",
            upstream=settings.AUTH_SERVICE_URL,
            error=type(exc).__name__,
        )
        return _error_response(
            request,
            status_code=503,
            code="AUTH_SERVICE_UNAVAILABLE",
            message="认证服务暂时不可用",
        )

    response_headers = {name: upstream.headers[name] for name in FORWARDED_RESPONSE_HEADERS if name in upstream.headers}
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=response_headers,
    )


@router.api_route(
    "/api/auth/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
)
async def proxy_auth(request: Request, path: str = "") -> Response:
    """代理认证服务控制面接口（登录 / 登出 / 凭证管理）。"""
    return await _proxy_auth_request(request)
