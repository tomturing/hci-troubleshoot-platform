"""
单元测试：ReAct 工具确认事务链路。
"""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from app.services.conversation_service import ConversationService
from shared.models.audit import Authorization


@pytest.fixture
def conversation_id() -> uuid.UUID:
    return uuid.UUID("00000000-0000-0000-0000-000000000301")


@pytest.fixture
def mock_service():
    """创建带 AgentClient 的 ConversationService。"""
    service = ConversationService(
        repository=MagicMock(),
        ai_registry=MagicMock(),
        kb_client=AsyncMock(),
        session_factory=MagicMock(),
        agent_client=MagicMock(),
    )
    service._agent_client.react_confirm = AsyncMock(return_value=True)
    service.repository.get_conversation = AsyncMock(return_value=MagicMock(case_id="Q123"))
    service.repository.add_message = AsyncMock()
    return service


def _install_tool_result_session(service: ConversationService, tool_res: MagicMock) -> MagicMock:
    """安装返回指定 tool_result 的异步 session mock。"""
    scalar_result = MagicMock()
    scalar_result.scalar_one_or_none.return_value = tool_res

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=scalar_result)
    mock_session.add = MagicMock()
    mock_session.commit = AsyncMock()

    mock_session_ctx = MagicMock()
    mock_session_ctx.__aenter__.return_value = mock_session
    service.session_factory = MagicMock(return_value=mock_session_ctx)
    return mock_session


@pytest.mark.asyncio
async def test_tool_confirm_validates_hash_and_writes_authorization(mock_service, conversation_id):
    """确认通过时必须校验 input_hash，并写入授权记录。"""
    tool_res = MagicMock()
    tool_res.input_hash = "hash-ok"
    tool_res.authorization_id = None
    tool_res.authorized_by = None
    mock_session = _install_tool_result_session(mock_service, tool_res)

    success = await mock_service.submit_interactive_response(
        conversation_id=conversation_id,
        kind="tool_confirm",
        request_id="exec-001",
        acp_session_id=str(conversation_id),
        outcome={"confirmed": True, "authorized_by": "alice", "input_hash": "hash-ok"},
        metadata=None,
    )

    assert success is True
    added_auth = mock_session.add.call_args.args[0]
    assert isinstance(added_auth, Authorization)
    assert added_auth.exec_id == "exec-001"
    assert added_auth.actor == "alice"
    assert added_auth.decision == "approve"
    assert added_auth.tool_input_hash == "hash-ok"
    assert tool_res.authorization_id == added_auth.auth_id
    assert tool_res.authorized_by == "alice"
    mock_session.commit.assert_awaited_once()
    mock_service._agent_client.react_confirm.assert_awaited_once_with(
        session_id=str(conversation_id),
        confirmed=True,
        authorized_by="alice",
        exec_id="exec-001",
    )


@pytest.mark.asyncio
async def test_tool_confirm_hash_mismatch_fails_closed(mock_service, conversation_id):
    """hash 不匹配时不得解锁 agent-service 的 confirm 队列。"""
    tool_res = MagicMock()
    tool_res.input_hash = "hash-db"
    mock_session = _install_tool_result_session(mock_service, tool_res)

    success = await mock_service.submit_interactive_response(
        conversation_id=conversation_id,
        kind="tool_confirm",
        request_id="exec-002",
        acp_session_id=str(conversation_id),
        outcome={"confirmed": True, "authorized_by": "alice", "input_hash": "hash-client"},
        metadata=None,
    )

    assert success is False
    mock_session.add.assert_not_called()
    mock_service._agent_client.react_confirm.assert_not_awaited()
