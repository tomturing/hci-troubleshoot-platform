"""
KB Service — hits 路由单元测试

覆盖：
- POST /api/kb/sop/{document_id}/hit
- POST /api/kb/kbd/{kbd_id}/hit
- POST /api/kb/kbd/{kbd_id}/hit/decrement

鉴权、404、正常 +1/-1 等场景均有覆盖。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.routes.hits import kbd_hit_router, set_dependencies, sop_hit_router
from fastapi import FastAPI
from fastapi.testclient import TestClient

# 测试用 token（与 config 默认值一致）
_VALID_TOKEN = "hci-dev-internal-token"
_AUTH_HEADER = {"Authorization": f"Bearer {_VALID_TOKEN}"}


@pytest.fixture
def app() -> FastAPI:
    """创建包含 hits 路由的最小 FastAPI 实例"""
    _app = FastAPI()
    _app.include_router(sop_hit_router)
    _app.include_router(kbd_hit_router)
    return _app


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app, raise_server_exceptions=True)


def _make_db_manager(row: tuple | None) -> MagicMock:
    """构造一个伪造的 DatabaseManager，execute 返回指定 row。"""
    mock_result = MagicMock()
    mock_result.one_or_none.return_value = row

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.commit = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    mock_db = MagicMock()
    mock_db.async_session_factory = MagicMock(return_value=mock_session)
    return mock_db


# --------------- 鉴权测试 ---------------


class TestAuth:
    def test_sop_hit_no_token_returns_401(self, client: TestClient):
        """缺少 Authorization header → 401"""
        set_dependencies(MagicMock())
        resp = client.post("/api/kb/sop/1/hit")
        assert resp.status_code == 401

    def test_kbd_hit_invalid_token_returns_401(self, client: TestClient):
        """Token 错误 → 401"""
        set_dependencies(MagicMock())
        resp = client.post("/api/kb/kbd/1/hit", headers={"Authorization": "Bearer wrong-token"})
        assert resp.status_code == 401

    def test_kbd_decrement_no_token_returns_401(self, client: TestClient):
        """缺少 Authorization header → 401"""
        set_dependencies(MagicMock())
        resp = client.post("/api/kb/kbd/1/hit/decrement")
        assert resp.status_code == 401


# --------------- SOP 命中测试 ---------------


class TestSopHit:
    def test_increment_success(self, client: TestClient):
        """正常 +1：返回 200 和正确的 hit_count"""
        db = _make_db_manager(row=(42, 5))
        set_dependencies(db)

        with patch("shared.utils.trace.get_current_trace_id", return_value="trace-001"):
            resp = client.post("/api/kb/sop/42/hit", headers=_AUTH_HEADER)

        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert body["document_id"] == 42
        assert body["hit_count"] == 5

    def test_increment_not_found_returns_404(self, client: TestClient):
        """文档不存在（DB 返回 None）→ 404"""
        db = _make_db_manager(row=None)
        set_dependencies(db)

        with patch("shared.utils.trace.get_current_trace_id", return_value="trace-002"):
            resp = client.post("/api/kb/sop/999/hit", headers=_AUTH_HEADER)

        assert resp.status_code == 404

    def test_db_not_ready_returns_503(self, client: TestClient):
        """db_manager 未注入 → 503"""
        set_dependencies(None)  # type: ignore[arg-type]
        resp = client.post("/api/kb/sop/1/hit", headers=_AUTH_HEADER)
        assert resp.status_code == 503


# --------------- KBD 命中测试 ---------------


class TestKbdHit:
    def test_increment_success(self, client: TestClient):
        """KBD 命中 +1：返回 200 和正确的 hit_count"""
        db = _make_db_manager(row=(7, 3))
        set_dependencies(db)

        with patch("shared.utils.trace.get_current_trace_id", return_value="trace-003"):
            resp = client.post("/api/kb/kbd/7/hit", headers=_AUTH_HEADER)

        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert body["kbd_id"] == 7
        assert body["hit_count"] == 3

    def test_increment_not_found_returns_404(self, client: TestClient):
        """KBD 条目不存在 → 404"""
        db = _make_db_manager(row=None)
        set_dependencies(db)

        with patch("shared.utils.trace.get_current_trace_id", return_value="trace-004"):
            resp = client.post("/api/kb/kbd/888/hit", headers=_AUTH_HEADER)

        assert resp.status_code == 404

    def test_db_not_ready_returns_503(self, client: TestClient):
        """db_manager 未注入 → 503"""
        set_dependencies(None)  # type: ignore[arg-type]
        resp = client.post("/api/kb/kbd/1/hit", headers=_AUTH_HEADER)
        assert resp.status_code == 503


# --------------- KBD 命中 decrement 测试 ---------------


class TestKbdHitDecrement:
    def test_decrement_success(self, client: TestClient):
        """KBD 命中 -1（GREATEST 保底 0）：返回 200 和正确的 hit_count"""
        db = _make_db_manager(row=(7, 0))  # hit_count 已被 GREATEST 处理为 0
        set_dependencies(db)

        with patch("shared.utils.trace.get_current_trace_id", return_value="trace-005"):
            resp = client.post("/api/kb/kbd/7/hit/decrement", headers=_AUTH_HEADER)

        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert body["kbd_id"] == 7
        assert body["hit_count"] == 0  # floor at 0

    def test_decrement_positive(self, client: TestClient):
        """hit_count > 0 时正常 -1"""
        db = _make_db_manager(row=(10, 4))
        set_dependencies(db)

        with patch("shared.utils.trace.get_current_trace_id", return_value="trace-006"):
            resp = client.post("/api/kb/kbd/10/hit/decrement", headers=_AUTH_HEADER)

        assert resp.status_code == 200
        assert resp.json()["hit_count"] == 4

    def test_decrement_not_found_returns_404(self, client: TestClient):
        """KBD 条目不存在 → 404"""
        db = _make_db_manager(row=None)
        set_dependencies(db)

        with patch("shared.utils.trace.get_current_trace_id", return_value="trace-007"):
            resp = client.post("/api/kb/kbd/777/hit/decrement", headers=_AUTH_HEADER)

        assert resp.status_code == 404

    def test_db_not_ready_returns_503(self, client: TestClient):
        """db_manager 未注入 → 503"""
        set_dependencies(None)  # type: ignore[arg-type]
        resp = client.post("/api/kb/kbd/1/hit/decrement", headers=_AUTH_HEADER)
        assert resp.status_code == 503
