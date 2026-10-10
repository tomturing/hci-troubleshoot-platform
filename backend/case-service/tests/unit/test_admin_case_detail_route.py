"""SRC admin 工单详情端点单测（无需数据库）。

管理后台无客户 client_id 身份，客户端路由 GET /{case_id} 的归属校验会对 admin
请求统一 404（staging 回归实录）。本测试固化 admin 专用端点 GET /admin/{case_id}
的契约：require_admin_token 保护 + 不做归属校验 + 404 语义，防止回退。
"""

from __future__ import annotations

from datetime import datetime

import pytest
from app.config import settings
from app.main import app
from app.routes.cases import get_case_service
from httpx import ASGITransport, AsyncClient

pytestmark = pytest.mark.unit

_ADMIN_HEADERS = {"Authorization": f"Bearer {settings.INTERNAL_API_TOKEN}"}


class _StubService:
    """替身服务：直接返回预置工单，绕开数据库。"""

    def __init__(self, case: dict | None):
        self._case = case

    async def get_case(self, case_id: str):
        return self._case


def _case_payload() -> dict:
    now = datetime(2026, 10, 10, 0, 0, 0)
    return {
        "case_id": "Q202610094215",
        "client_id": "client-random-abc",
        "status": "in_progress",
        "title": "工单详情回归测试",
        "description": None,
        "created_at": now,
        "updated_at": now,
        "closed_at": None,
        "trace_id": "trace-admin-detail-001",
    }


@pytest.mark.asyncio
async def test_admin_detail_returns_case_without_ownership_check():
    """admin 详情端点返回任意归属工单（无客户归属校验）"""
    app.dependency_overrides[get_case_service] = lambda: _StubService(_case_payload())
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/cases/admin/Q202610094215", headers=_ADMIN_HEADERS)
    finally:
        app.dependency_overrides.pop(get_case_service, None)

    assert resp.status_code == 200
    assert resp.json()["case_id"] == "Q202610094215"
    # 归属是随机客户 ID 而非 admin 身份——admin 端点必须不做归属校验
    assert resp.json()["client_id"] == "client-random-abc"


@pytest.mark.asyncio
async def test_admin_detail_missing_case_is_404():
    app.dependency_overrides[get_case_service] = lambda: _StubService(None)
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/cases/admin/Q-NOPE", headers=_ADMIN_HEADERS)
    finally:
        app.dependency_overrides.pop(get_case_service, None)

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Case not found"


@pytest.mark.asyncio
async def test_admin_detail_requires_admin_token():
    """未携带管理员凭证必须 403，匿名/客户身份不可读任意工单"""
    app.dependency_overrides[get_case_service] = lambda: _StubService(_case_payload())
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/cases/admin/Q202610094215")
    finally:
        app.dependency_overrides.pop(get_case_service, None)

    assert resp.status_code == 403
