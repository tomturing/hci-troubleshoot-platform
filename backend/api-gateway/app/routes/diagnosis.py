"""Diagnosis Service（诊断服务）控制面安全代理。"""

import re
import secrets
import uuid

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from shared.observability.logger import get_logger

from app.config import settings

router = APIRouter(tags=["diagnosis-proxy"])
logger = get_logger("gateway-diagnosis-proxy")

MAX_CONTROL_PLANE_BODY_BYTES = 1024 * 1024
TRUSTED_CONTEXT_PATTERN = re.compile(r"^[A-Za-z0-9._:@-]{1,128}$")
FORWARDED_REQUEST_HEADERS = (
    "content-type",
    "accept",
    "idempotency-key",
    "if-match",
    "traceparent",
    "tracestate",
)
FORWARDED_RESPONSE_HEADERS = (
    "content-type",
    "content-disposition",
    "cache-control",
    "etag",
    "idempotent-replayed",
    "x-artifact-sha256",
    "x-signature-algorithm",
    "x-signature-key-id",
    "x-detached-signature",
    "x-public-key-base64",
    "x-public-key-fingerprint",
    "x-root-public-key-fingerprint",
    "x-revocation-next-update",
)


def _error_response(request: Request, *, status_code: int, code: str, message: str) -> JSONResponse:
    """返回与诊断服务一致的错误外壳。"""

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


def _trusted_upstream_headers(request: Request) -> dict[str, str] | JSONResponse:
    """验证内部调用方并构造最小可信转发头。"""

    authorization = request.headers.get("Authorization", "")
    scheme, _, supplied_token = authorization.partition(" ")
    if settings.DIAGNOSIS_IDENTITY_MODE == "oidc":
        if scheme.lower() != "bearer" or not supplied_token.strip():
            return _error_response(request, status_code=401, code="UNAUTHORIZED", message="缺少正式 Bearer Token")
        headers = {"Authorization": authorization}
        for name in FORWARDED_REQUEST_HEADERS:
            if value := request.headers.get(name):
                headers[name] = value
        return headers

    configured_token = settings.INTERNAL_API_TOKEN.strip()
    if not configured_token:
        return _error_response(
            request,
            status_code=503,
            code="IDENTITY_PROVIDER_UNAVAILABLE",
            message="API Gateway 内部身份配置不可用",
        )

    if scheme.lower() != "bearer" or not supplied_token.strip():
        return _error_response(request, status_code=401, code="UNAUTHORIZED", message="缺少 Bearer Token")
    if not secrets.compare_digest(supplied_token.strip(), configured_token):
        return _error_response(request, status_code=403, code="FORBIDDEN", message="内部接口令牌无效")

    tenant_id = request.headers.get("X-Tenant-ID", "").strip()
    actor_id = request.headers.get("X-Actor-ID", "").strip()
    if not TRUSTED_CONTEXT_PATTERN.fullmatch(tenant_id):
        return _error_response(
            request,
            status_code=422,
            code="INVALID_TENANT_CONTEXT",
            message="内部调用必须提供合法的 X-Tenant-ID",
        )
    if not TRUSTED_CONTEXT_PATTERN.fullmatch(actor_id):
        return _error_response(
            request,
            status_code=422,
            code="INVALID_ACTOR_CONTEXT",
            message="内部调用必须提供合法的 X-Actor-ID",
        )

    headers = {
        "Authorization": f"Bearer {configured_token}",
        "X-Tenant-ID": tenant_id,
        "X-Actor-ID": actor_id,
    }
    for name in FORWARDED_REQUEST_HEADERS:
        if value := request.headers.get(name):
            headers[name] = value
    return headers


async def _proxy_diagnosis_request(request: Request) -> Response:
    """将受控诊断 API 请求转发到 diagnosis-service。"""

    headers = _trusted_upstream_headers(request)
    if isinstance(headers, JSONResponse):
        return headers

    content_length = request.headers.get("Content-Length")
    if content_length:
        try:
            if int(content_length) > MAX_CONTROL_PLANE_BODY_BYTES:
                return _error_response(
                    request,
                    status_code=413,
                    code="CONTROL_PLANE_BODY_TOO_LARGE",
                    message="诊断控制面请求体不能超过 1 MiB",
                )
        except ValueError:
            return _error_response(
                request,
                status_code=400,
                code="INVALID_CONTENT_LENGTH",
                message="Content-Length 格式不合法",
            )

    body = await request.body()
    if len(body) > MAX_CONTROL_PLANE_BODY_BYTES:
        return _error_response(
            request,
            status_code=413,
            code="CONTROL_PLANE_BODY_TOO_LARGE",
            message="诊断控制面请求体不能超过 1 MiB",
        )

    upstream_url = f"{settings.DIAGNOSIS_SERVICE_URL.rstrip('/')}{request.url.path}"
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
            event="gateway_diagnosis_proxy_error",
            message="Diagnosis Service 请求失败",
            upstream=settings.DIAGNOSIS_SERVICE_URL,
            error=type(exc).__name__,
        )
        return _error_response(
            request,
            status_code=503,
            code="DIAGNOSIS_SERVICE_UNAVAILABLE",
            message="诊断服务暂时不可用",
        )

    response_headers = {name: upstream.headers[name] for name in FORWARDED_RESPONSE_HEADERS if name in upstream.headers}
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=response_headers,
    )


@router.api_route("/api/diagnosis-sessions", methods=["POST"])
@router.api_route("/api/diagnosis-scenarios", methods=["GET"])
@router.api_route("/api/diagnosis-sessions/{path:path}", methods=["GET"])
@router.api_route("/api/diagnosis-sessions/{path:path}", methods=["POST"])
@router.api_route("/api/internal/diagnosis-sessions", methods=["GET"])
@router.api_route("/api/internal/collection-profiles", methods=["GET"])
@router.api_route("/api/internal/collection-profiles/{path:path}", methods=["GET"])
@router.api_route("/api/internal/collection-profiles/{path:path}", methods=["POST"])
@router.api_route("/api/internal/collection-profiles/{path:path}", methods=["PUT"])
@router.api_route("/api/internal/collection-plans", methods=["GET"])
@router.api_route("/api/internal/collection-plans/{path:path}", methods=["POST"])
@router.api_route("/api/internal/collector-artifacts", methods=["GET"])
@router.api_route("/api/internal/collector-artifacts/{path:path}", methods=["POST"])
@router.api_route("/api/internal/kbd-collection-impact/{path:path}", methods=["GET"])
@router.api_route("/api/internal/collectors", methods=["GET"])
@router.api_route("/api/internal/collectors/{path:path}", methods=["GET"])
@router.api_route("/api/internal/collectors/{path:path}", methods=["POST"])
@router.api_route("/api/internal/collectors/{path:path}", methods=["PUT"])
@router.api_route("/api/internal/diagnosis-sessions/{path:path}", methods=["GET"])
@router.api_route("/api/internal/diagnosis-sessions/{path:path}", methods=["POST"])
@router.api_route("/api/internal/diagnosis-security/{path:path}", methods=["GET"])
@router.api_route("/api/internal/offline-signal-mappings", methods=["GET"])
@router.api_route("/api/internal/offline-signal-mappings/{path:path}", methods=["PUT"])
@router.api_route("/api/internal/offline-resource-sync/{path:path}", methods=["GET"])
@router.api_route("/api/internal/offline-resource-sync/{path:path}", methods=["POST"])
async def proxy_diagnosis_control_plane(request: Request, path: str = "") -> Response:
    """代理离线诊断控制面接口；不接收证据大文件。"""

    return await _proxy_diagnosis_request(request)
