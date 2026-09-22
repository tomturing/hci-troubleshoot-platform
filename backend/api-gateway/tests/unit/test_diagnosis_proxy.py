"""Diagnosis Service（诊断服务）安全代理测试（B1：网关身份重签）。

身份模型要点：
- 管理员（Authorization: Bearer INTERNAL_API_TOKEN）保留自报租户/操作者能力。
- 普通访客一律降级为 `customer` 角色，归属键取服务端签发的身份 Cookie，
  客户端自报的租户、操作者与角色头一律无效。
- `/api/internal/*` 与 bundle-migration 属管理面，必须管理员凭证。
"""

from unittest.mock import AsyncMock, MagicMock, patch

from app.config import settings
from app.middleware.identity import IdentityMiddleware
from app.routes.diagnosis import (
    ACTOR_CUSTOMER_HEADER,
    ACTOR_ROLES_HEADER,
    MAX_CONTROL_PLANE_BODY_BYTES,
    router,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient


def build_client() -> TestClient:
    """构造带身份中间件的诊断代理测试应用。"""

    app = FastAPI()
    app.add_middleware(IdentityMiddleware)
    app.include_router(router)
    return TestClient(app)


def admin_headers(**extra: str) -> dict[str, str]:
    """构造管理员请求头（内部令牌使中间件标记 is_admin）。"""

    return {
        "Authorization": f"Bearer {settings.INTERNAL_API_TOKEN}",
        "X-Tenant-ID": "tenant-a",
        "X-Actor-ID": "diagnosis-worker",
        **extra,
    }


def mock_upstream(*, status_code: int = 200, content: bytes = b'{"ok":true}', headers: dict | None = None):
    """构造 httpx 上游响应替身。"""

    response = MagicMock()
    response.status_code = status_code
    response.content = content
    response.headers = headers or {"content-type": "application/json"}
    return response


@patch("app.routes.diagnosis.httpx.AsyncClient")
def test_admin_forwards_trusted_context_and_concurrency_headers(mock_client_cls):
    """管理员自报租户与操作者被转发，角色缺省为平台管理员。"""

    client = AsyncMock()
    mock_client_cls.return_value.__aenter__.return_value = client
    client.request.return_value = mock_upstream(status_code=200, headers={"etag": '"2"'})

    response = build_client().put(
        "/api/internal/collectors/collector.safe",
        json={"collector_id": "collector.safe"},
        headers=admin_headers(**{"Idempotency-Key": "request-1", "If-Match": '"1"'}),
    )

    assert response.status_code == 200
    assert response.headers["etag"] == '"2"'
    _, upstream_url = client.request.call_args.args
    assert upstream_url.endswith("/api/internal/collectors/collector.safe")
    upstream_headers = client.request.call_args.kwargs["headers"]
    assert upstream_headers["Authorization"] == f"Bearer {settings.INTERNAL_API_TOKEN}"
    assert upstream_headers["X-Tenant-ID"] == "tenant-a"
    assert upstream_headers["X-Actor-ID"] == "diagnosis-worker"
    assert upstream_headers[ACTOR_ROLES_HEADER] == "platform_admin"
    assert upstream_headers["idempotency-key"] == "request-1"
    assert upstream_headers["if-match"] == '"1"'


@patch("app.routes.diagnosis.httpx.AsyncClient")
def test_admin_supplied_roles_are_limited_to_internal_allowlist(mock_client_cls):
    """管理员自报的角色必须落在内部角色白名单内，非法角色回退为平台管理员。"""

    client = AsyncMock()
    mock_client_cls.return_value.__aenter__.return_value = client
    client.request.return_value = mock_upstream()

    response = build_client().get(
        "/api/internal/collectors",
        headers=admin_headers(**{ACTOR_ROLES_HEADER: "support_engineer, attacker"}),
    )

    assert response.status_code == 200
    upstream_headers = client.request.call_args.kwargs["headers"]
    assert upstream_headers[ACTOR_ROLES_HEADER] == "support_engineer"


@patch("app.routes.diagnosis.httpx.AsyncClient")
def test_customer_identity_is_derived_from_cookie_and_ignores_self_reported_context(mock_client_cls):
    """普通访客身份只能来自 Cookie；自报租户、操作者、角色一律不得提权。"""

    client = AsyncMock()
    mock_client_cls.return_value.__aenter__.return_value = client
    client.request.return_value = mock_upstream(content=b"[]")

    response = build_client().get(
        "/api/diagnosis-scenarios",
        headers={
            "X-Tenant-ID": "attacker-tenant",
            "X-Actor-ID": "platform_admin",
            ACTOR_ROLES_HEADER: "platform_admin",
            "X-Client-ID": "someone-else",
        },
    )

    assert response.status_code == 200
    upstream_headers = client.request.call_args.kwargs["headers"]
    assert upstream_headers["Authorization"] == f"Bearer {settings.INTERNAL_API_TOKEN}"
    assert upstream_headers["X-Tenant-ID"] == "default"
    assert upstream_headers[ACTOR_ROLES_HEADER] == "customer"
    assert upstream_headers["X-Actor-ID"].startswith("cust-")
    assert upstream_headers[ACTOR_CUSTOMER_HEADER] == upstream_headers["X-Actor-ID"].removeprefix("cust-")
    assert upstream_headers["X-Actor-ID"] != "platform_admin"


@patch("app.routes.diagnosis.httpx.AsyncClient")
def test_customer_cannot_reach_internal_control_plane(mock_client_cls):
    """客户身份访问 internal 管理面一律 403，且不得调用上游。"""

    response = build_client().get("/api/internal/collectors")

    assert response.status_code == 403
    mock_client_cls.assert_not_called()


@patch("app.routes.diagnosis.httpx.AsyncClient")
def test_customer_cannot_reach_bundle_migration(mock_client_cls):
    """Bundle 迁移属管理面，客户身份不得访问。"""

    response = build_client().post("/api/v1/bundle-migration/migrate", json={})

    assert response.status_code == 403
    mock_client_cls.assert_not_called()


@patch("app.routes.diagnosis.httpx.AsyncClient")
def test_anonymous_visitor_is_auto_issued_customer_identity(mock_client_cls):
    """匿名访客由中间件签发身份后按客户身份代理，保持自服务排障形态。"""

    client = AsyncMock()
    mock_client_cls.return_value.__aenter__.return_value = client
    client.request.return_value = mock_upstream(content=b"[]")

    response = build_client().get("/api/diagnosis-scenarios")

    assert response.status_code == 200
    upstream_headers = client.request.call_args.kwargs["headers"]
    assert upstream_headers[ACTOR_ROLES_HEADER] == "customer"


@patch("app.routes.diagnosis.httpx.AsyncClient")
def test_download_preserves_signature_and_binary_headers(mock_client_cls):
    """下载代理必须保留签名、公钥指纹和附件响应头。"""

    client = AsyncMock()
    mock_client_cls.return_value.__aenter__.return_value = client
    client.request.return_value = mock_upstream(
        content=b'{"schema_version":"1.0"}\n',
        headers={
            "content-type": "application/vnd.hci.collector+json",
            "content-disposition": 'attachment; filename="collector.hci-collector.json"',
            "x-artifact-sha256": "a" * 64,
            "x-detached-signature": "signature",
            "x-public-key-fingerprint": "b" * 64,
        },
    )

    response = build_client().get(
        "/api/diagnosis-sessions/00000000-0000-0000-0000-000000000001/"
        "collector-artifacts/00000000-0000-0000-0000-000000000002/download",
        headers=admin_headers(),
    )

    assert response.status_code == 200
    assert response.content == b'{"schema_version":"1.0"}\n'
    assert response.headers["x-artifact-sha256"] == "a" * 64
    assert response.headers["x-detached-signature"] == "signature"
    assert response.headers["x-public-key-fingerprint"] == "b" * 64


@patch("app.routes.diagnosis.httpx.AsyncClient")
def test_available_scenarios_route_is_proxied(mock_client_cls):
    """客户侧可用场景接口必须代理到诊断服务。"""

    client = AsyncMock()
    mock_client_cls.return_value.__aenter__.return_value = client
    client.request.return_value = mock_upstream(content=b"[]")

    response = build_client().get("/api/diagnosis-scenarios", headers=admin_headers())

    assert response.status_code == 200
    _, upstream_url = client.request.call_args.args
    assert upstream_url.endswith("/api/diagnosis-scenarios")


@patch("app.routes.diagnosis.httpx.AsyncClient")
def test_verification_bundle_preserves_trust_and_revocation_headers(mock_client_cls):
    """验证包代理必须保留受信根指纹和吊销清单更新时间。"""

    client = AsyncMock()
    mock_client_cls.return_value.__aenter__.return_value = client
    client.request.return_value = mock_upstream(
        content=b"PK\x03\x04",
        headers={
            "content-type": "application/zip",
            "content-disposition": 'attachment; filename="verification.zip"',
            "cache-control": "private, no-store",
            "x-root-public-key-fingerprint": "c" * 64,
            "x-revocation-next-update": "2026-07-30T10:00:00+00:00",
        },
    )

    response = build_client().get(
        "/api/diagnosis-sessions/00000000-0000-0000-0000-000000000001/"
        "collector-artifacts/00000000-0000-0000-0000-000000000002/verification-bundle",
        headers=admin_headers(),
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-root-public-key-fingerprint"] == "c" * 64
    assert response.headers["x-revocation-next-update"] == "2026-07-30T10:00:00+00:00"


@patch("app.routes.diagnosis.httpx.AsyncClient")
def test_proxy_rejects_oversized_control_plane_body(mock_client_cls):
    """控制面代理不得演变为大文件转发通道。"""

    response = build_client().post(
        "/api/diagnosis-sessions",
        content=b"x" * (MAX_CONTROL_PLANE_BODY_BYTES + 1),
        headers=admin_headers(**{"Content-Type": "application/octet-stream"}),
    )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "CONTROL_PLANE_BODY_TOO_LARGE"
    mock_client_cls.assert_not_called()


@patch("app.routes.diagnosis.httpx.AsyncClient")
def test_oidc_mode_forwards_token_but_never_browser_identity_headers(mock_client_cls, monkeypatch):
    """OIDC 模式由下游验签，网关不把浏览器伪造的租户和角色头升级为可信上下文。"""

    monkeypatch.setattr(settings, "DIAGNOSIS_IDENTITY_MODE", "oidc")
    client = AsyncMock()
    mock_client_cls.return_value.__aenter__.return_value = client
    client.request.return_value = mock_upstream()

    response = build_client().get(
        "/api/diagnosis-sessions/00000000-0000-0000-0000-000000000001",
        headers={
            "Authorization": "Bearer signed.oidc.token",
            "X-Tenant-ID": "attacker",
            "X-Actor-ID": "attacker",
        },
    )

    assert response.status_code == 200
    upstream_headers = client.request.call_args.kwargs["headers"]
    assert upstream_headers["Authorization"] == "Bearer signed.oidc.token"
    assert "X-Tenant-ID" not in upstream_headers
    assert "X-Actor-ID" not in upstream_headers
