"""日志收集「故障场景」字典的网关代理测试。

字典有两个入口、两条互不相通的身份链路，本组测试锁的就是"不能混用"：

1. 客户读 ``GET /api/log-collect/scenarios``：必须要求客户身份，并把身份 HMAC 重签
   给 case-service；它**不能**落到 ``/tasks{path}`` 前缀代理（那条按工单归属做 BOLA）；
2. 管理端 ``/api/v1/log-collect-scenarios``：必须要求管理员凭证，改用内部令牌重签，
   操作者从 ``X-Admin-User`` 或 JWT ``sub`` 透传给下游落审计。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.config import settings
from app.routes import log_collect, log_collect_admin
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from shared.security.signature import verify_client_identity


def _install(mock_client_cls, fake_request) -> None:
    instance = AsyncMock()
    instance.request = fake_request
    ctx = AsyncMock()
    ctx.__aenter__.return_value = instance
    ctx.__aexit__.return_value = False
    mock_client_cls.return_value = ctx


def _patch_upstream(module_name: str, *, status_code: int = 200, content: bytes = b'{"scenarios":[]}'):
    response = MagicMock()
    response.status_code = status_code
    response.content = content
    response.headers = {"content-type": "application/json"}
    captured: dict = {}

    async def fake_request(method, url, **kwargs):
        captured.update(method=method, url=url, **kwargs)
        return response

    return patch(f"{module_name}.httpx.AsyncClient"), captured, fake_request


def _customer_app() -> TestClient:
    """客户端：模拟已签发 Cookie 的用户（中间件口径：state.client_id）。"""
    app = FastAPI()

    @app.middleware("http")
    async def fake_identity(request: Request, call_next):  # noqa: ANN001
        request.state.client_id = "client-a"
        request.state.is_admin = False
        return await call_next(request)

    app.include_router(log_collect.router)
    return TestClient(app)


def _admin_app(*, authed: bool = True) -> TestClient:
    app = FastAPI()

    @app.middleware("http")
    async def fake_identity(request: Request, call_next):  # noqa: ANN001
        request.state.client_id = None
        request.state.is_admin = authed
        # 验签后写入的操作者身份（GatewayActor）；网关用它兜底 X-Admin-User
        request.state.auth = SimpleNamespace(user_id="admin-7") if authed else None
        return await call_next(request)

    app.include_router(log_collect_admin.router)
    return TestClient(app)


@pytest.mark.asyncio
async def test_customer_scenarios_requires_identity_and_resigns_headers():
    client = _customer_app()
    patch_target, captured, fake_request = _patch_upstream("app.routes.log_collect")

    with patch_target as mock_client_cls:
        _install(mock_client_cls, fake_request)
        response = client.get("/api/log-collect/scenarios")

    assert response.status_code == 200
    # 必须直达字典端点，不能走任务前缀（否则会被工单归属校验拦成 404）
    assert captured["url"].endswith("/api/log-collect/scenarios")
    assert "/tasks" not in captured["url"]
    headers = captured["headers"]
    assert headers["X-Client-ID"] == "client-a"
    assert verify_client_identity(headers, settings.INTERNAL_API_TOKEN) == "client-a"


@pytest.mark.asyncio
async def test_customer_scenarios_rejects_anonymous_without_upstream_call():
    """未认证不放行：匿名枚举平台能力等于把字典泄露给任何人。"""
    app = FastAPI()

    @app.middleware("http")
    async def fake_identity(request: Request, call_next):  # noqa: ANN001
        request.state.client_id = None
        request.state.is_admin = False
        return await call_next(request)

    app.include_router(log_collect.router)
    client = TestClient(app)

    patch_target, captured, fake_request = _patch_upstream("app.routes.log_collect")
    with patch_target as mock_client_cls:
        _install(mock_client_cls, fake_request)
        response = client.get("/api/log-collect/scenarios")

    assert response.status_code == 401
    assert captured == {}


@pytest.mark.asyncio
async def test_admin_requires_admin_identity():
    client = _admin_app(authed=False)

    patch_target, captured, fake_request = _patch_upstream("app.routes.log_collect_admin")
    with patch_target as mock_client_cls:
        _install(mock_client_cls, fake_request)
        response = client.get("/api/v1/log-collect-scenarios")

    assert response.status_code == 403
    assert captured == {}


@pytest.mark.asyncio
async def test_admin_list_forwards_internal_token_and_actor():
    client = _admin_app()

    patch_target, captured, fake_request = _patch_upstream("app.routes.log_collect_admin")
    with patch_target as mock_client_cls:
        _install(mock_client_cls, fake_request)
        response = client.get("/api/v1/log-collect-scenarios")

    assert response.status_code == 200
    assert captured["method"] == "GET"
    assert captured["url"].endswith("/internal/log-collect/scenarios")
    headers = captured["headers"]
    # 下游校验的是内部令牌，绝不是客户端 JWT
    assert headers["Authorization"] == f"Bearer {settings.INTERNAL_API_TOKEN}"
    # 前端没传 X-Admin-User 时用验签后的 JWT sub 兜底，保证审计里有操作者
    assert headers["X-Admin-User"] == "admin-7"


@pytest.mark.asyncio
async def test_admin_update_forwards_path_value_and_body():
    client = _admin_app()

    patch_target, captured, fake_request = _patch_upstream("app.routes.log_collect_admin")
    with patch_target as mock_client_cls:
        _install(mock_client_cls, fake_request)
        response = client.put(
            "/api/v1/log-collect-scenarios/other",
            json={"summary": "只收基础系统日志"},
            headers={"X-Admin-User": "ops-zhang"},
        )

    assert response.status_code == 200
    assert captured["method"] == "PUT"
    assert captured["url"].endswith("/internal/log-collect/scenarios/other")
    assert captured["json"] == {"summary": "只收基础系统日志"}
    # 显式操作者优先于 JWT sub
    assert captured["headers"]["X-Admin-User"] == "ops-zhang"


@pytest.mark.asyncio
async def test_admin_proxy_reports_503_on_upstream_error():
    """下游不可达不能吞成空字典：前端要据此回落到 bridge 短名并提示重试。"""
    import httpx

    client = _admin_app()

    async def boom(*_args, **_kwargs):
        raise httpx.ConnectError("connection refused")

    patch_target, _captured, _fake = _patch_upstream("app.routes.log_collect_admin")
    with patch_target as mock_client_cls:
        _install(mock_client_cls, boom)
        response = client.get("/api/v1/log-collect-scenarios")

    assert response.status_code == 503
