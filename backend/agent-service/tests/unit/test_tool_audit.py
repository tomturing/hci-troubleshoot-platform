import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from app.services.tool_audit import DbAuditService, ToolAuditService
from shared.database.postgres import Base
from shared.models.audit import ToolResult
from sqlalchemy.orm import configure_mappers


def test_sqlalchemy_metadata_compiles_successfully():
    """验证 SQLAlchemy 所有表元数据（包括 ToolResult 关联）能成功编译。"""
    assert "tool_result" in Base.metadata.tables
    configure_mappers()


@pytest.mark.asyncio
async def test_tool_audit_service_write_success():
    """验证 ToolAuditService 能够将工具执行信息拼装为 ToolResult 对象并成功写入 DB。"""
    # Mock db session 和 factory
    mock_session = AsyncMock()
    mock_session.__aenter__.return_value = mock_session
    mock_session_factory = MagicMock(return_value=mock_session)

    # 初始化服务
    ToolAuditService.initialize(mock_session_factory)

    # 模拟数据
    audit_id = str(uuid.uuid4())
    session_id = str(uuid.uuid4())
    tool_name = "acli_system_top"
    tool_args = {"node_ip": "10.0.0.1"}
    started_at = datetime.now(UTC)
    completed_at = datetime.now(UTC)
    duration_ms = 150

    # 触发写入
    await ToolAuditService.write_tool_audit(
        audit_id=audit_id,
        session_id=session_id,
        tool_name=tool_name,
        tool_args=tool_args,
        risk_level=1,
        policy="auto",
        result="some output",
        error=None,
        started_at=started_at,
        completed_at=completed_at,
        duration_ms=duration_ms,
        authorized_by="operator-01",
        trace_id="trace-123",
        step_no=2,
    )

    # 验证 session.add()
    mock_session.add.assert_called_once()
    added_obj = mock_session.add.call_args[0][0]

    assert isinstance(added_obj, ToolResult)
    assert added_obj.id == audit_id
    assert added_obj.conversation_id == uuid.UUID(session_id)
    assert added_obj.tool_name == tool_name
    assert added_obj.tool_type == "acli"
    assert added_obj.step_no == 2
    assert added_obj.risk_level == 1
    assert added_obj.policy == "auto"
    assert added_obj.authorized_by == "operator-01"
    assert added_obj.input_json == tool_args
    assert added_obj.output_json == {"data": "some output"}
    assert added_obj.error is None
    assert added_obj.duration_ms == duration_ms
    assert added_obj.trace_id == "trace-123"

    # 验证 session.commit()
    mock_session.commit.assert_called_once()


@pytest.mark.asyncio
async def test_db_audit_service_adapter():
    """验证 DbAuditService 适配器能正确转发 write 调用。"""
    mock_session = AsyncMock()
    mock_session.__aenter__.return_value = mock_session
    mock_session_factory = MagicMock(return_value=mock_session)

    # 初始化服务
    ToolAuditService.initialize(mock_session_factory)

    adapter = DbAuditService()
    audit_id = str(uuid.uuid4())
    session_id = str(uuid.uuid4())
    started_at = datetime.now(UTC)
    completed_at = datetime.now(UTC)

    # 调用适配器接口
    await adapter.write(
        audit_id=audit_id,
        session_id=session_id,
        tool_name="get_sop_node",
        tool_args={"node_id": "n-2"},
        risk_level=1,
        policy="notify",
        result={"status": "ok"},
        error=None,
        started_at=started_at,
        completed_at=completed_at,
        duration_ms=80,
    )

    # 验证 session.add()
    mock_session.add.assert_called_once()
    added_obj = mock_session.add.call_args[0][0]
    assert added_obj.tool_type == "sop"
    assert added_obj.output_json == {"data": "{'status': 'ok'}"}
