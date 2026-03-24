"""
AuditService 单元测试

覆盖：
  - write() 正常写入审计记录
  - write() 在 DB 连接失败时不抛异常（仅打 error 日志）
  - result 字段超过 2000 字符时被截断
"""

import os
import sys

_svc = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _svc not in sys.path:
    sys.path.insert(0, _svc)

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.services.audit_service import RESULT_MAX_CHARS, AuditService


@pytest.fixture
def mock_db():
    db = AsyncMock()
    db.add = MagicMock()
    return db


@pytest.fixture
def service(mock_db):
    return AuditService(db=mock_db)


def _make_write_kwargs(**overrides):
    """构造 write() 的默认参数"""
    now = datetime.now(UTC)
    defaults = dict(
        audit_id="audit-001",
        session_id="sid-001",
        tool_name="get_active_alerts",
        tool_args={"limit": 10},
        risk_level=1,
        policy="auto",
        result={"alarms": [], "total": 0},
        error=None,
        started_at=now,
        completed_at=now,
        duration_ms=120,
    )
    defaults.update(overrides)
    return defaults


class TestAuditServiceWrite:

    @pytest.mark.asyncio
    async def test_write_calls_db_add_and_commit(self, service, mock_db):
        """正常情况下，write() 调用 db.add 和 db.commit"""
        with patch("app.services.audit_service.ToolAuditLog") as MockLog:
            await service.write(**_make_write_kwargs())

        mock_db.add.assert_called_once()
        mock_db.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_db_error_does_not_raise(self, service, mock_db):
        """DB 连接中断时，write() 不应向上抛异常（仅 error 日志）"""
        mock_db.commit.side_effect = Exception("数据库连接断开")

        # 不应抛出任何异常
        with patch("app.services.audit_service.ToolAuditLog"):
            await service.write(**_make_write_kwargs())   # 应静默失败

    @pytest.mark.asyncio
    async def test_result_truncated_to_max_chars(self, service, mock_db):
        """超长 result 应被截断到 RESULT_MAX_CHARS"""
        long_result = "x" * 5000   # 5000 字符，超过 2000

        captured_log = None

        class CaptureMock:
            def __init__(self, **kwargs):
                nonlocal captured_log
                captured_log = kwargs

        with patch("app.services.audit_service.ToolAuditLog", CaptureMock):
            await service.write(**_make_write_kwargs(result=long_result))

        # result 字段应被截断
        assert captured_log is not None
        result_data = captured_log.get("result", {})
        assert len(result_data.get("data", "")) <= RESULT_MAX_CHARS

    @pytest.mark.asyncio
    async def test_result_none_stored_as_none(self, service, mock_db):
        """result=None 时，ToolAuditLog.result 字段应为 None"""
        captured_log = None

        class CaptureMock:
            def __init__(self, **kwargs):
                nonlocal captured_log
                captured_log = kwargs

        with patch("app.services.audit_service.ToolAuditLog", CaptureMock):
            await service.write(**_make_write_kwargs(result=None))

        assert captured_log["result"] is None

    @pytest.mark.asyncio
    async def test_trace_id_optional(self, service, mock_db):
        """trace_id 为可选，不传时应为 None"""
        captured_log = None

        class CaptureMock:
            def __init__(self, **kwargs):
                nonlocal captured_log
                captured_log = kwargs

        with patch("app.services.audit_service.ToolAuditLog", CaptureMock):
            await service.write(**_make_write_kwargs())   # 不传 trace_id

        assert captured_log["trace_id"] is None
