"""Diagnosis Service（诊断服务）控制面安全代理（B1：网关身份重签）。

身份模型（internal 模式）：
- 网关以自身 INTERNAL_API_TOKEN 向下游声明"调用者是网关"，终端用户身份
  由服务端签发的身份 Cookie 派生，**不再信任客户端自报的租户/操作者/角色头**。
- 管理员（is_admin）保留自报租户与操作者能力，用于 hci-sim/运维以指定身份执行操作。
- 普通访客一律降级为 `customer` 角色，并携带无法伪造的归属键（Cookie client_id），
  下游据此执行工单归属校验。
"""

import re
import uuid

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from shared.observability.logger import get_logger

from app.config import settings
from app.security.gateway_auth import require_admin

router = APIRouter(tags=["diagnosis-proxy"])
logger = get_logger("gateway-diagnosis-proxy")

MAX_CONTROL_PLANE_BODY_BYTES = 1024 * 1024
TRUSTED_CONTEXT_PATTERN = re.compile(r"^[A-Za-z0-9._:@-]{1,128}$")

# 网关↔诊断服务的身份重签约定（B1）
ACTOR_ROLES_HEADER = "X-Actor-Roles"
ACTOR_CUSTOMER_HEADER = "X-Actor-Customer-ID"
CUSTOMER_ROLE = "customer"
ADMIN_ROLE = "platform_admin"
INTERNAL_ROLE_ALLOWLIST = frozenset({"platform_admin", "support_engineer", "diagnosis_worker"})
FALLBACK_TENANT_ID = "default"
FALLBACK_ADMIN_ACTOR_ID = "gateway-admin"
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


def _admin_actor_roles(request: Request) -> str:
    """管理员可自报角色；角色值必须在内部角色白名单内，缺省提权为平台管理员。"""

    supplied = request.headers.get(ACTOR_ROLES_HEADER, "").strip().replace(",", " ")
    roles = {item for item in supplied.split() if item in INTERNAL_ROLE_ALLOWLIST}
    return " ".join(sorted(roles)) or ADMIN_ROLE


def _resigned_upstream_headers(request: Request) -> dict[str, str] | JSONResponse:
    """按网关自身身份重签下游内部身份头。

    internal 模式下网关不再把客户端自报的 `X-Tenant-ID` / `X-Actor-ID` 当作可信上下文：
    普通访客统一派生成客户身份，管理员保留自报能力（格式非法一律 422）。
    """

    if settings.DIAGNOSIS_IDENTITY_MODE == "oidc":
        authorization = request.headers.get("Authorization", "")
        scheme, _, supplied_token = authorization.partition(" ")
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

    client_id = getattr(request.state, "client_id", None)
    if not client_id:
        return _error_response(
            request,
            status_code=401,
            code="UNAUTHORIZED",
            message="缺少服务端签发身份",
        )

    if getattr(request.state, "is_admin", False):
        tenant_id = request.headers.get("X-Tenant-ID", "").strip()
        actor_id = request.headers.get("X-Actor-ID", "").strip()
        if tenant_id and not TRUSTED_CONTEXT_PATTERN.fullmatch(tenant_id):
            return _error_response(
                request,
                status_code=422,
                code="INVALID_TENANT_CONTEXT",
                message="内部调用必须提供合法的 X-Tenant-ID",
            )
        if actor_id and not TRUSTED_CONTEXT_PATTERN.fullmatch(actor_id):
            return _error_response(
                request,
                status_code=422,
                code="INVALID_ACTOR_CONTEXT",
                message="内部调用必须提供合法的 X-Actor-ID",
            )
        headers = {
            "Authorization": f"Bearer {configured_token}",
            "X-Tenant-ID": tenant_id or FALLBACK_TENANT_ID,
            "X-Actor-ID": actor_id or FALLBACK_ADMIN_ACTOR_ID,
            ACTOR_ROLES_HEADER: _admin_actor_roles(request),
        }
    else:
        headers = {
            "Authorization": f"Bearer {configured_token}",
            "X-Tenant-ID": FALLBACK_TENANT_ID,
            "X-Actor-ID": f"cust-{client_id}",
            ACTOR_ROLES_HEADER: CUSTOMER_ROLE,
            ACTOR_CUSTOMER_HEADER: client_id,
        }

    for name in FORWARDED_REQUEST_HEADERS:
        if value := request.headers.get(name):
            headers[name] = value
    return headers


async def _proxy_diagnosis_request(request: Request) -> Response:
    """将受控诊断 API 请求转发到 diagnosis-service。"""

    headers = _resigned_upstream_headers(request)
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


# ---- 用户面：客户离线诊断自服务链路（身份由 Cookie 派生为 customer 角色）----
@router.api_route("/api/diagnosis-sessions", methods=["POST"])
@router.api_route("/api/diagnosis-scenarios", methods=["GET"])
@router.api_route("/api/diagnosis-scenarios/semantic-advice", methods=["GET", "POST"])
@router.api_route("/api/diagnosis-sessions/{path:path}", methods=["GET"])
@router.api_route("/api/diagnosis-sessions/{path:path}", methods=["POST"])
async def proxy_diagnosis_user_plane(request: Request, path: str = "") -> Response:
    """代理客户侧离线诊断接口；不接收证据大文件。"""

    return await _proxy_diagnosis_request(request)


# ---- 管理面：internal 控制面（B1：必须由管理员凭证进入，客户链路 0 调用）----
@router.api_route("/api/internal/diagnosis-sessions", methods=["GET"], dependencies=[Depends(require_admin)])
@router.api_route("/api/internal/collection-profiles", methods=["GET"], dependencies=[Depends(require_admin)])
@router.api_route(
    "/api/internal/collection-profiles/{path:path}",
    methods=["GET"],
    dependencies=[Depends(require_admin)],
)
@router.api_route(
    "/api/internal/collection-profiles/{path:path}",
    methods=["POST"],
    dependencies=[Depends(require_admin)],
)
@router.api_route(
    "/api/internal/collection-profiles/{path:path}",
    methods=["PUT"],
    dependencies=[Depends(require_admin)],
)
@router.api_route("/api/internal/collection-plans", methods=["GET"], dependencies=[Depends(require_admin)])
@router.api_route(
    "/api/internal/collection-plans/{path:path}",
    methods=["POST"],
    dependencies=[Depends(require_admin)],
)
@router.api_route("/api/internal/collector-artifacts", methods=["GET"], dependencies=[Depends(require_admin)])
@router.api_route(
    "/api/internal/collector-artifacts/{path:path}",
    methods=["POST"],
    dependencies=[Depends(require_admin)],
)
@router.api_route(
    "/api/internal/kbd-collection-impact/{path:path}",
    methods=["GET"],
    dependencies=[Depends(require_admin)],
)
@router.api_route("/api/internal/collectors", methods=["GET"], dependencies=[Depends(require_admin)])
@router.api_route(
    "/api/internal/collectors/{path:path}",
    methods=["GET"],
    dependencies=[Depends(require_admin)],
)
@router.api_route(
    "/api/internal/collectors/{path:path}",
    methods=["POST"],
    dependencies=[Depends(require_admin)],
)
@router.api_route(
    "/api/internal/collectors/{path:path}",
    methods=["PUT"],
    dependencies=[Depends(require_admin)],
)
@router.api_route(
    "/api/internal/diagnosis-sessions/{path:path}",
    methods=["GET"],
    dependencies=[Depends(require_admin)],
)
@router.api_route(
    "/api/internal/diagnosis-sessions/{path:path}",
    methods=["POST"],
    dependencies=[Depends(require_admin)],
)
@router.api_route(
    "/api/internal/diagnosis-security/{path:path}",
    methods=["GET"],
    dependencies=[Depends(require_admin)],
)
@router.api_route("/api/internal/offline-signal-mappings", methods=["GET"], dependencies=[Depends(require_admin)])
@router.api_route(
    "/api/internal/offline-signal-mappings/{path:path}",
    methods=["PUT"],
    dependencies=[Depends(require_admin)],
)
@router.api_route(
    "/api/internal/offline-resource-sync/{path:path}",
    methods=["GET"],
    dependencies=[Depends(require_admin)],
)
@router.api_route(
    "/api/internal/offline-resource-sync/{path:path}",
    methods=["POST"],
    dependencies=[Depends(require_admin)],
)
# Bundle 迁移路由（离线资源迁移工具，归属管理面）
@router.api_route("/api/v1/bundle-migration/health", methods=["GET"], dependencies=[Depends(require_admin)])
@router.api_route("/api/v1/bundle-migration/migrate", methods=["POST"], dependencies=[Depends(require_admin)])
@router.api_route("/api/v1/bundle-migration/version", methods=["GET"], dependencies=[Depends(require_admin)])
async def proxy_diagnosis_control_plane(request: Request, path: str = "") -> Response:
    """代理离线诊断管理面接口；要求管理员凭证，不接收证据大文件。"""

    return await _proxy_diagnosis_request(request)
