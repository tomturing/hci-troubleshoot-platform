"""日志收集控制面网关代理测试。

回归背景：#1192~#1204 在 case-service 侧上线了 ``/api/log-collect/tasks``，
但网关没有注册该前缀，staging 实测 404（直连 case-service 为 401），导致
customer-ui 日志收集面板一创建任务就失败。本组测试锁定四件事：

1. 路径与 query 原样转发（含 task_id/force）；
2. 身份由服务端 Cookie 派生并 HMAC 重签，客户端自报身份不被信任；
3. 下游状态码与 RFC 7807 错误体原样透传；
4. 控制面请求体超限在网关拒绝，数据面不经过这里。
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.config import settings
from app.middleware.identity import IdentityMiddleware
from app.routes.log_collect import MAX_CONTROL_PLANE_BODY_BYTES, router
from fastapi import FastAPI
from fastapi.testclient import TestClient
from shared.security.signature import verify_client_identity


def build_client() -> TestClient:
    """构造挂载身份中间件的日志收集代理测试应用。"""
    app = FastAPI()
    app.add_middleware(IdentityMiddleware)
    app.include_router(router)
    return TestClient(app)


def mock_upstream(*, status_code: int = 200, content: bytes = b'{"ok":true}', headers: dict | None = None):
    """构造 httpx 上游响应替身。"""
    response = MagicMock()
    response.status_code = status_code
    response.content = content
    response.headers = headers or {"content-type": "application/json"}
    return response


def patch_upstream(response):
    """把 AsyncClient 替换为返回固定响应的替身，并返回捕获容器。"""
    captured: dict = {}

    async def fake_request(method, url, **kwargs):
        captured.update(method=method, url=url, **kwargs)
        return response

    patch_target = patch("app.routes.log_collect.httpx.AsyncClient")
    return patch_target, captured, fake_request


def _install(mock_client_cls, fake_request):
    instance = AsyncMock()
    instance.request = fake_request
    ctx = AsyncMock()
    ctx.__aenter__.return_value = instance
    ctx.__aexit__.return_value = False
    mock_client_cls.return_value = ctx


@pytest.mark.asyncio
async def test_proxy_resigns_cookie_identity_and_forwards_query():
    """必须用服务端签发身份重签下游，并转发 case_id 等查询参数。"""
    client = build_client()
    patch_target, captured, fake_request = patch_upstream(mock_upstream())

    with patch_target as mock_client_cls:
        _install(mock_client_cls, fake_request)
        response = client.get("/api/log-collect/tasks", params={"case_id": "Q2026102200001"})

    assert response.status_code == 200
    assert captured["method"] == "GET"
    assert captured["url"].endswith("/api/log-collect/tasks")
    assert captured["params"] == {"case_id": "Q2026102200001"}
    headers = captured["headers"]
    signed_client_id = verify_client_identity(headers, settings.INTERNAL_API_TOKEN)
    assert signed_client_id
    assert headers["X-Client-ID"] == signed_client_id


@pytest.mark.asyncio
async def test_proxy_forwards_task_id_path_and_force_query():
    """task 子路径（cancel/relay/uploads）必须完整转发，避免落到错误端点。"""
    client = build_client()
    patch_target, captured, fake_request = patch_upstream(mock_upstream(status_code=202, content=b"{}"))

    with patch_target as mock_client_cls:
        _install(mock_client_cls, fake_request)
        response = client.post(
            "/api/log-collect/tasks/3f1a8d2c-0000-4000-8000-000000000001/relay",
            params={"force": "true"},
        )

    assert response.status_code == 202
    assert captured["method"] == "POST"
    assert captured["url"].endswith("/api/log-collect/tasks/3f1a8d2c-0000-4000-8000-000000000001/relay")
    assert captured["params"] == {"force": "true"}


@pytest.mark.asyncio
async def test_proxy_passes_through_rfc7807_error_without_rewrite():
    """下游 404/409 与业务错误体必须原样返回，网关不包装。"""
    problem = b'{"detail":{"code":"LC-PLUGIN-007","status":409,"title":"conflict"}}'
    client = build_client()
    patch_target, captured, fake_request = patch_upstream(mock_upstream(status_code=409, content=problem))

    with patch_target as mock_client_cls:
        _install(mock_client_cls, fake_request)
        response = client.post("/api/log-collect/tasks", json={"case_id": "Q2026102200001"})

    assert response.status_code == 409
    assert response.content == problem
    assert json.loads(captured["content"]) == {"case_id": "Q2026102200001"}


@pytest.mark.asyncio
async def test_proxy_does_not_trust_client_supplied_identity_header():
    """客户端自报 X-Client-ID 不得覆盖服务端派生身份（BOLA 前提）。"""
    client = build_client()
    patch_target, captured, fake_request = patch_upstream(mock_upstream())

    with patch_target as mock_client_cls:
        _install(mock_client_cls, fake_request)
        client.get("/api/log-collect/tasks", params={"case_id": "Q1"}, headers={"X-Client-ID": "attacker"})

    headers = captured["headers"]
    assert headers["X-Client-ID"] != "attacker"
    assert verify_client_identity(headers, settings.INTERNAL_API_TOKEN) == headers["X-Client-ID"]


@pytest.mark.asyncio
async def test_proxy_rejects_oversized_control_plane_body_without_upstream_call():
    """超过 1 MiB 的控制面请求体必须在网关拒绝，不能落到 case-service。"""
    client = build_client()
    patch_target, captured, fake_request = patch_upstream(mock_upstream())

    with patch_target as mock_client_cls:
        _install(mock_client_cls, fake_request)
        response = client.post(
            "/api/log-collect/tasks",
            content=b"x" * (MAX_CONTROL_PLANE_BODY_BYTES + 1),
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 413
    assert captured == {}


@pytest.mark.asyncio
async def test_merge_complete_uses_long_timeout():
    """分片合并是 20 GiB 级流式落盘，必须使用长超时而非 30s。"""
    client = build_client()
    patch_target, captured, fake_request = patch_upstream(mock_upstream(content=b"{}"))

    with patch_target as mock_client_cls:
        _install(mock_client_cls, fake_request)
        client.post(
            "/api/log-collect/tasks/3f1a8d2c-0000-4000-8000-000000000001/uploads/complete",
            json={"parts": []},
        )
        assert mock_client_cls.call_args.kwargs["timeout"] == 600.0

    assert captured["url"].endswith("/uploads/complete")
