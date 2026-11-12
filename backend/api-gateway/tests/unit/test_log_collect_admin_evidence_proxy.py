"""管理端日志收集「证据端点」的网关代理测试。

锁住两条边界：内部令牌重签（不是客户身份链路）与路径段 UUID 白名单
（用户可控字符串不能直接拼进上游 URL）。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.routes import log_collect_admin
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

TASK_ID = "11111111-1111-1111-1111-111111111111"
LOG_ID = "22222222-2222-2222-2222-222222222222"


def _app(*, authed: bool = True) -> TestClient:
    app = FastAPI()

    @app.middleware("http")
    async def fake_identity(request: Request, call_next):  # noqa: ANN001
        request.state.client_id = None
        request.state.is_admin = authed
        request.state.auth = SimpleNamespace(user_id="admin-7") if authed else None
        return await call_next(request)

    app.include_router(log_collect_admin.tasks_router)
    return TestClient(app)


@pytest.fixture
def upstream():
    response = MagicMock()
    response.status_code = 200
    response.content = b'{"items":[]}'
    response.headers = {"content-type": "application/json"}
    captured: dict = {}

    async def fake_request(method, url, **kwargs):
        captured.update(method=method, url=url, **kwargs)
        return response

    instance = AsyncMock()
    instance.request = fake_request
    ctx = AsyncMock()
    ctx.__aenter__.return_value = instance
    ctx.__aexit__.return_value = False
    with patch.object(log_collect_admin.httpx, "AsyncClient", return_value=ctx):
        yield captured


@pytest.mark.asyncio
async def test_detail_uses_internal_token_and_canonical_uuid(upstream):
    response = _app().get(f"/api/v1/log-collect/admin/tasks/{TASK_ID}")

    assert response.status_code == 200
    assert upstream["url"].endswith(f"/internal/log-collect/admin/tasks/{TASK_ID}")
    assert upstream["headers"]["Authorization"].startswith("Bearer ")
    assert "X-Client-ID" not in upstream["headers"]


@pytest.mark.asyncio
async def test_transfer_metrics_preserves_query(upstream):
    response = _app().get(f"/api/v1/log-collect/admin/tasks/{TASK_ID}/transfer-metrics?limit=5")

    assert response.status_code == 200
    assert upstream["url"].endswith(f"/internal/log-collect/admin/tasks/{TASK_ID}/transfer-metrics?limit=5")


@pytest.mark.asyncio
async def test_process_log_content_builds_nested_path(upstream):
    response = _app().get(f"/api/v1/log-collect/admin/tasks/{TASK_ID}/process-logs/{LOG_ID}/content")

    assert response.status_code == 200
    assert upstream["url"].endswith(f"/internal/log-collect/admin/tasks/{TASK_ID}/process-logs/{LOG_ID}/content")


@pytest.mark.asyncio
async def test_path_traversal_is_rejected_before_upstream(upstream):
    response = _app().get("/api/v1/log-collect/admin/tasks/..%2F..%2Fcases/process-logs/bad/content")

    assert response.status_code in (400, 404)
    assert upstream == {}


@pytest.mark.asyncio
async def test_anonymous_is_rejected_without_upstream(upstream):
    response = _app(authed=False).get(f"/api/v1/log-collect/admin/tasks/{TASK_ID}")

    assert response.status_code == 403
    assert upstream == {}
