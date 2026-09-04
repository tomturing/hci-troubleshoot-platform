from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
import pytest
from fastapi import HTTPException, Request

from app.config import settings
from app.routes import signal_assets


class _SessionContext:
    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _MappingsResult:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None


class _ScalarResult:
    def __init__(self, val):
        self._val = val

    def scalar_one(self):
        return self._val


def _make_request(token: str | None = None) -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "headers": [(b"authorization", f"Bearer {token}".encode())] if token else [],
    }
    return Request(scope)


@pytest.mark.asyncio
async def test_signal_assets_auth_failure():
    req = _make_request(token="wrong-token")
    with pytest.raises(HTTPException) as exc_info:
        await signal_assets.list_templates(req)
    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_signal_assets_list_templates_success():
    req = _make_request(token=settings.INTERNAL_API_TOKEN)
    mock_db = MagicMock()
    mock_session = AsyncMock()

    now = datetime.now(UTC)
    fake_rows = [
        {
            "id": 1,
            "tool_name": "qfk_log",
            "category": "backend",
            "description": "采集后台日志",
            "acquire_schema": {"type": "object"},
            "allowed_matcher_types": ["regex", "contains"],
            "variable_protocol": {"inputs": ["HOST"]},
            "anti_patterns": ["禁止无正则全量扫日志"],
            "is_active": True,
            "trace_id": "trace-1",
            "created_at": now,
            "updated_at": now,
        }
    ]

    mock_session.execute = AsyncMock(return_value=_MappingsResult(fake_rows))
    mock_db.async_session_factory = MagicMock(return_value=_SessionContext(mock_session))
    signal_assets.set_dependencies(mock_db)

    res = await signal_assets.list_templates(req, category="backend")
    assert res["total"] == 1
    assert res["items"][0]["tool_name"] == "qfk_log"
    assert res["items"][0]["category"] == "backend"
    assert res["items"][0]["allowed_matcher_types"] == ["regex", "contains"]


@pytest.mark.asyncio
async def test_signal_assets_list_best_practices_success():
    req = _make_request(token=settings.INTERNAL_API_TOKEN)
    mock_db = MagicMock()
    mock_session = AsyncMock()

    now = datetime.now(UTC)
    fake_rows = [
        {
            "id": 10,
            "template_id": 1,
            "tool_name": "qfk_log",
            "pattern_category": "IO_HANG",
            "source_kbd_id": 1001,
            "support_id": "37150",
            "raw_evidence": "kernel: blocked for more than 120 seconds",
            "signal_json": {"title": "检查内核IO挂起", "matcher": {"type": "contains"}},
            "design_notes": "高频卡死排查样本",
            "completeness_score": 10,
            "is_active": True,
            "trace_id": "trace-bp-1",
            "created_at": now,
            "updated_at": now,
        }
    ]

    # total count query followed by items query
    mock_session.execute = AsyncMock(side_effect=[_ScalarResult(1), _MappingsResult(fake_rows)])
    mock_db.async_session_factory = MagicMock(return_value=_SessionContext(mock_session))
    signal_assets.set_dependencies(mock_db)

    res = await signal_assets.list_best_practices(req, tool_name="qfk_log", limit=20, offset=0)
    assert res["total"] == 1
    assert res["items"][0]["support_id"] == "37150"
    assert res["items"][0]["signal_title"] == "检查内核IO挂起"
    assert res["items"][0]["completeness_score"] == 10


@pytest.mark.asyncio
async def test_signal_assets_get_best_practice_detail():
    req = _make_request(token=settings.INTERNAL_API_TOKEN)
    mock_db = MagicMock()
    mock_session = AsyncMock()

    now = datetime.now(UTC)
    fake_row = {
        "id": 10,
        "template_id": 1,
        "tool_name": "qfk_log",
        "pattern_category": "IO_HANG",
        "source_kbd_id": 1001,
        "support_id": "37150",
        "raw_evidence": "kernel: blocked for more than 120 seconds",
        "signal_json": {"title": "检查内核IO挂起"},
        "design_notes": "高频卡死排查样本",
        "completeness_score": 10,
        "is_active": True,
        "trace_id": "trace-bp-1",
        "created_at": now,
        "updated_at": now,
    }

    mock_session.execute = AsyncMock(return_value=_MappingsResult([fake_row]))
    mock_db.async_session_factory = MagicMock(return_value=_SessionContext(mock_session))
    signal_assets.set_dependencies(mock_db)

    detail = await signal_assets.get_best_practice(req, practice_id=10)
    assert detail["id"] == 10
    assert detail["signal_title"] == "检查内核IO挂起"

    # 测试不存在返回 404
    mock_session.execute = AsyncMock(return_value=_MappingsResult([]))
    with pytest.raises(HTTPException) as exc_info:
        await signal_assets.get_best_practice(req, practice_id=999)
    assert exc_info.value.status_code == 404
