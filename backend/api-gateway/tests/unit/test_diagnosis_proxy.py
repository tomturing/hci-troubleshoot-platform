"""Diagnosis Service（诊断服务）安全代理测试。"""

from unittest.mock import AsyncMock, MagicMock, patch

from app.config import settings
from app.routes.diagnosis import MAX_CONTROL_PLANE_BODY_BYTES, router
from fastapi import FastAPI
from fastapi.testclient import TestClient


def build_client() -> TestClient:
    """构造仅包含诊断代理的测试应用。"""

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def trusted_headers(**extra: str) -> dict[str, str]:
    """构造可信内部请求头。"""

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
def test_proxy_forwards_only_trusted_context_and_concurrency_headers(mock_client_cls):
    """代理验证内部令牌并透传租户、操作者、幂等键和 If-Match。"""

    client = AsyncMock()
    mock_client_cls.return_value.__aenter__.return_value = client
    client.request.return_value = mock_upstream(status_code=200, headers={"etag": '"2"'})

    response = build_client().put(
        "/api/internal/collectors/collector.safe",
        json={"collector_id": "collector.safe"},
        headers=trusted_headers(**{"Idempotency-Key": "request-1", "If-Match": '"1"'}),
    )

    assert response.status_code == 200
    assert response.headers["etag"] == '"2"'
    _, upstream_url = client.request.call_args.args
    assert upstream_url.endswith("/api/internal/collectors/collector.safe")
    upstream_headers = client.request.call_args.kwargs["headers"]
    assert upstream_headers["Authorization"] == f"Bearer {settings.INTERNAL_API_TOKEN}"
    assert upstream_headers["X-Tenant-ID"] == "tenant-a"
    assert upstream_headers["X-Actor-ID"] == "diagnosis-worker"
    assert upstream_headers["idempotency-key"] == "request-1"
    assert upstream_headers["if-match"] == '"1"'


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
        headers=trusted_headers(),
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

    response = build_client().get("/api/diagnosis-scenarios", headers=trusted_headers())

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
        headers=trusted_headers(),
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-root-public-key-fingerprint"] == "c" * 64
    assert response.headers["x-revocation-next-update"] == "2026-07-30T10:00:00+00:00"


@patch("app.routes.diagnosis.httpx.AsyncClient")
def test_proxy_rejects_missing_internal_token_without_calling_upstream(mock_client_cls):
    """缺少内部令牌时不得调用诊断服务。"""

    response = build_client().get(
        "/api/diagnosis-sessions/00000000-0000-0000-0000-000000000001",
        headers={"X-Tenant-ID": "tenant-a", "X-Actor-ID": "diagnosis-worker"},
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"
    mock_client_cls.assert_not_called()


@patch("app.routes.diagnosis.httpx.AsyncClient")
def test_proxy_rejects_oversized_control_plane_body(mock_client_cls):
    """控制面代理不得演变为大文件转发通道。"""

    response = build_client().post(
        "/api/diagnosis-sessions",
        content=b"x" * (MAX_CONTROL_PLANE_BODY_BYTES + 1),
        headers=trusted_headers(**{"Content-Type": "application/octet-stream"}),
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
