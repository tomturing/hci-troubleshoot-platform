"""P1 网关鉴权加固测试。

覆盖 2026-09-22 P1 批次接入 require_user / require_admin 的路由族：
- assistants / audit-logs / kb（search、sop/match 用户级；管理家族 admin 级）
- signal dry-run（用户级）/ hci-sim 控制面（admin 级）
- WebSocket 握手身份一致性（Cookie 身份必须与路径 client_id 一致）

矩阵：匿名 → 401/403；用户 Cookie → 放行；admin 令牌 → 放行。
"""

import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketDisconnect

_svc = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _svc not in sys.path:
    sys.path.insert(0, _svc)

from app.config import settings
from app.main import app
from shared.security.identity import IDENTITY_COOKIE_NAME, issue_identity

ADMIN_HEADERS = {"Authorization": f"Bearer {settings.INTERNAL_API_TOKEN}"}


def _user_client() -> TestClient:
    """携带合法服务端签发身份 Cookie 的客户端（模拟浏览器）。"""
    _, cookie_value = issue_identity(settings.INTERNAL_API_TOKEN)
    client = TestClient(app)
    client.cookies.update({IDENTITY_COOKIE_NAME: cookie_value})
    return client


def _anon_client() -> TestClient:
    return TestClient(app)


# ============ 匿名请求必须被拒（admin 族 403） ============
# 注意设计边界：require_user 级路由对匿名 HTTP 请求不会 401——IdentityMiddleware
# 会对无 Cookie 的访客自动签发服务端身份（无登录模型的既定取舍，P0 设计如此）。
# user 级门禁的价值是强制请求携带可验签的可信身份；真正的安全边界是 admin 族。


@pytest.mark.parametrize(
    ("method", "url"),
    [
        ("get", "/api/kb/categories"),
        ("get", "/api/kb/catalogs"),
        ("get", "/api/v1/vm-console/captures"),
        ("get", "/api/v1/signal-assets/templates"),
        ("get", "/api/v1/kbd/pending"),
        ("get", "/api/v1/sop"),
        ("post", "/api/v1/kb/ingest"),
        ("get", "/api/hci-sim/v1/control-plane/bundles"),
    ],
)
def test_anonymous_requests_rejected_on_admin_routes(method, url):
    client = _anon_client()
    response = client.request(method, url, json={})
    assert response.status_code == 403, f"{method} {url} → {response.status_code}"


# ============ user 级路由：匿名首访自动签发身份并放行（按设计） ============


def test_anonymous_first_visit_gets_issued_identity_and_passes_assistants():
    client = _anon_client()
    downstream = httpx.Response(200, json={"assistants": [], "show_selector": False})
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=downstream)):
        response = client.get("/api/assistants/")
    assert response.status_code == 200
    # 身份已由网关签发（Set-Cookie），后续请求即可信
    assert "hci_client_id" in response.headers.get("set-cookie", "")


def test_anonymous_first_visit_passes_kb_search():
    client = _anon_client()
    downstream = httpx.Response(200, json={"results": []})
    with patch("httpx.AsyncClient.request", new=AsyncMock(return_value=downstream)):
        response = client.post("/api/v1/kb/search", json={"query": "cpu"})
    assert response.status_code == 200


def test_anonymous_first_visit_passes_audit_logs():
    client = _anon_client()
    downstream = httpx.Response(200, json=[])
    with patch("httpx.AsyncClient.request", new=AsyncMock(return_value=downstream)):
        response = client.get("/api/audit-logs")
    assert response.status_code == 200


def test_anonymous_first_visit_reaches_dry_run_validation():
    """dry-run 匿名请求应越过鉴权到达业务校验（422 缺 dataset），而非 401/403。"""
    client = _anon_client()
    response = client.post("/api/v1/signals/dry-run", json={})
    assert response.status_code == 422


# ============ 合法用户（Cookie）放行：代理正常转发下游 ============


def test_user_cookie_passes_assistants():
    client = _user_client()
    downstream = httpx.Response(200, json={"assistants": [], "show_selector": False})
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=downstream)):
        response = client.get("/api/assistants/")
    assert response.status_code == 200


def test_user_cookie_passes_audit_logs():
    client = _user_client()
    downstream = httpx.Response(200, json=[])
    with patch("httpx.AsyncClient.request", new=AsyncMock(return_value=downstream)):
        response = client.get("/api/audit-logs")
    assert response.status_code == 200


def test_user_cookie_passes_kb_search():
    client = _user_client()
    downstream = httpx.Response(200, json={"results": []})
    with patch("httpx.AsyncClient.request", new=AsyncMock(return_value=downstream)):
        response = client.post("/api/v1/kb/search", json={"query": "cpu"})
    assert response.status_code == 200


# ============ admin 令牌（无 Cookie）放行 admin 家族 ============


def test_admin_token_passes_hci_sim_bundles():
    client = TestClient(app)
    client.headers.update(ADMIN_HEADERS)
    downstream = httpx.Response(200, json={"bundles": []})
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=downstream)):
        response = client.get("/api/hci-sim/v1/control-plane/bundles")
    assert response.status_code == 200


def test_admin_token_passes_kb_categories():
    client = TestClient(app)
    client.headers.update(ADMIN_HEADERS)
    downstream = httpx.Response(200, json=[])
    with patch("httpx.AsyncClient.request", new=AsyncMock(return_value=downstream)):
        response = client.get("/api/kb/categories")
    assert response.status_code == 200


def test_admin_token_passes_vm_console_captures():
    client = TestClient(app)
    client.headers.update(ADMIN_HEADERS)
    downstream = httpx.Response(200, json=[])
    with patch("httpx.AsyncClient.request", new=AsyncMock(return_value=downstream)):
        response = client.get("/api/v1/vm-console/captures")
    assert response.status_code == 200


def test_invalid_admin_token_rejected_on_hci_sim():
    client = TestClient(app)
    client.headers.update({"Authorization": "Bearer wrong-token"})
    response = client.get("/api/hci-sim/v1/control-plane/bundles")
    assert response.status_code == 403


# ============ WebSocket 握手身份一致性（P0 漏网通道的回归） ============


def _install_session_manager():
    manager = MagicMock()
    manager.create_session = AsyncMock()
    manager.close_session = AsyncMock()
    from app.routes import websocket as ws_module

    ws_module.set_session_manager(manager)
    return manager


def test_ws_rejects_without_identity_cookie():
    _install_session_manager()
    client = _anon_client()
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/" + "a" * 32):
            pass


def test_ws_rejects_mismatched_identity():
    """持自己 Cookie 却连他人 client_id 的通道 → 必须拒绝（IDOR 回归）。"""
    _install_session_manager()
    _, attacker_cookie = issue_identity(settings.INTERNAL_API_TOKEN)
    client = TestClient(app)
    client.cookies.update({IDENTITY_COOKIE_NAME: attacker_cookie})
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/" + "b" * 32):
            pass


def test_ws_accepts_matching_identity():
    _install_session_manager()
    client_id, cookie_value = issue_identity(settings.INTERNAL_API_TOKEN)
    client = TestClient(app)
    client.cookies.update({IDENTITY_COOKIE_NAME: cookie_value})
    with client.websocket_connect(f"/ws/{client_id}") as ws:
        ws.close()
