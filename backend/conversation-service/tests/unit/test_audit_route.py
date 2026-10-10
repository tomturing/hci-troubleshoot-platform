"""list_audit_logs 路由单元测试：验证 v6.2 重构后按 conversation_id 过滤（修复 500）。

回归背景：v6.2 把 tool_audit_log 表废弃，ToolAuditLog 别名指向 tool_result 表，
tool_result 以 conversation_id 关联会话、无 session_id 列。原 list_audit_logs 仍按
ToolAuditLog.session_id 过滤，构建查询时抛 AttributeError → 500。前端按会话过滤时
传入的 session_id 实际为 conversation_id，修复后按 conversation_id 列查询。
"""

import os
import sys
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

_svc = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _svc not in sys.path:
    sys.path.insert(0, _svc)

from app.routes.audit import list_audit_logs


@pytest.mark.asyncio
async def test_list_audit_logs_filters_by_conversation_id():
    """修复前按 ToolAuditLog.session_id 过滤（ToolResult 无此列）→ AttributeError 500；
    修复后按 conversation_id 过滤，应正常返回且不抛异常。"""
    conv_id = uuid.uuid4()
    mock_log = MagicMock()
    mock_log.id = "tool-1"
    mock_log.conversation_id = conv_id
    mock_log.tool_name = "get_active_alerts"
    mock_log.tool_args = {}
    mock_log.risk_level = 1
    mock_log.policy = "auto"
    mock_log.authorized_by = None
    mock_log.result = {}
    mock_log.error = None
    mock_log.started_at = None
    mock_log.completed_at = None
    mock_log.duration_ms = 10
    mock_log.trace_id = None

    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = [mock_log]

    mock_db = AsyncMock()
    mock_db.execute.return_value = mock_result

    resp = await list_audit_logs(
        session_id=str(conv_id), tool_name=None, risk_level=None, limit=50, offset=0, db=mock_db
    )
    assert resp["total"] == 1
    # 返回的 session_id 字段填的是 conversation_id（前端实际传入值）
    assert resp["items"][0]["session_id"] == str(conv_id)


@pytest.mark.asyncio
async def test_list_audit_logs_no_filter_returns_all():
    """不传 session_id 时不应抛异常（回归：空过滤条件路径）。"""
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = []
    mock_db = AsyncMock()
    mock_db.execute.return_value = mock_result

    resp = await list_audit_logs(tool_name=None, risk_level=None, limit=50, offset=0, db=mock_db)
    assert resp["total"] == 0
    assert resp["items"] == []
