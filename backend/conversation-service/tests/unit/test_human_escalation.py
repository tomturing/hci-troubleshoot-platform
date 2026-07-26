"""Agent escalation 到前端交互契约的单元测试。"""

import uuid

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
