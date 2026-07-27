"""Agent escalation 到前端交互契约的单元测试。"""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from app.services.conversation_service import ConversationService


def test_escalation_is_visible_human_interaction():
    conversation_id = uuid.UUID("518f3c64-93dd-4c9e-a247-9b6411882ee9")
    event = {
        "type": "escalation",
        "reason": "KBD 必需关键信号未形成完整 PASS 证据链",
        "context": {"category_id": "虚拟机-003", "snapshot_id": "snap-1"},
    }

    result = ConversationService._escalation_interactive_event(conversation_id, event)

    assert result["requestId"] == f"escalation-{conversation_id}"
    assert result["acpSessionId"] == str(conversation_id)
    assert result["kind"] == "human_escalation"
    assert result["prompt"] == event["reason"]
    assert result["metadata"] == event["context"]
    assert result["options"] == [{"optionId": "ack", "name": "我知道了"}]


@pytest.mark.asyncio
async def test_human_escalation_ack_is_persisted_locally_without_agent_client():
    """HTP 的人工升级卡没有 ACP 请求，确认动作不得误发给 ops-agent。"""
    conversation_id = uuid.UUID("518f3c64-93dd-4c9e-a247-9b6411882ee9")
    repository = MagicMock()
    repository.get_conversation = AsyncMock(return_value=MagicMock(case_id="Q2026072769668"))
    repository.add_message = AsyncMock()
    service = ConversationService(
        repository=repository,
        ai_registry=MagicMock(),
        agent_client=None,
    )

    success = await service.submit_interactive_response(
        conversation_id=conversation_id,
        kind="human_escalation",
        request_id=f"escalation-{conversation_id}",
        acp_session_id=str(conversation_id),
        outcome={"outcome": "selected", "optionId": "ack", "optionLabel": "我知道了"},
    )

    assert success is True
    repository.add_message.assert_awaited_once()
    assert repository.add_message.await_args.kwargs["content"] == "[操作选择] 我知道了"
    assert repository.add_message.await_args.kwargs["metadata"]["interactive_kind"] == "human_escalation"


@pytest.mark.asyncio
async def test_human_escalation_rejects_non_ack_response():
    conversation_id = uuid.UUID("518f3c64-93dd-4c9e-a247-9b6411882ee9")
    repository = MagicMock()
    service = ConversationService(repository=repository, ai_registry=MagicMock(), agent_client=None)

    success = await service.submit_interactive_response(
        conversation_id=conversation_id,
        kind="human_escalation",
        request_id=f"escalation-{conversation_id}",
        acp_session_id=str(conversation_id),
        outcome={"outcome": "selected", "optionId": "unexpected"},
    )

    assert success is False
    repository.add_message.assert_not_called()
