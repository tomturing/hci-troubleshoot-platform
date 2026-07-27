import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from app.services.conversation_service import ConversationService


@pytest.mark.asyncio
async def test_kbd_tool_call_and_result_upsert_by_exec_id():
    stored = {}
    session = AsyncMock()

    async def get_record(_model, record_id):
        return stored.get(record_id)

    def add_record(record):
        stored[record.id] = record

    session.get = AsyncMock(side_effect=get_record)
    session.add = MagicMock(side_effect=add_record)
    session.commit = AsyncMock()
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=session)
    context.__aexit__ = AsyncMock(return_value=False)

    service = ConversationService(
        repository=MagicMock(),
        ai_registry=MagicMock(),
        kb_client=AsyncMock(),
        session_factory=MagicMock(return_value=context),
        agent_client=MagicMock(),
    )
    conversation_id = uuid.UUID("00000000-0000-0000-0000-000000000749")
    exec_id = "608a39f8-cae5-5e34-b96a-c1466b4e688f"

    await service._record_tool_call(
        conversation_id=conversation_id,
        case_id="Q2026072747493",
        stage="tool_call",
        metadata={
            "exec_id": exec_id,
            "tool_name": "qkv_task",
            "args": {"keyword": "启动虚拟机失败", "limit": 1},
            "status": "running",
            "risk_level": 1,
            "policy": "auto",
        },
    )
    await service._record_tool_call(
        conversation_id=conversation_id,
        case_id="Q2026072747493",
        stage="tool_result",
        metadata={
            "exec_id": exec_id,
            "tool_name": "qkv_task",
            "args": {"keyword": "启动虚拟机失败", "limit": 1},
            "result": "matched=1",
            "status": "success",
        },
    )

    assert list(stored) == [exec_id]
    record = stored[exec_id]
    assert record.case_id == "Q2026072747493"
    assert record.input_json["limit"] == 1
    assert record.output_json == "matched=1"
    assert record.status == "success"
    assert record.completed_at is not None
    assert session.commit.await_count == 2


@pytest.mark.asyncio
async def test_resume_stream_persists_and_forwards_tool_lifecycle():
    conversation_id = uuid.UUID("00000000-0000-0000-0000-000000000750")
    repository = MagicMock()
    repository.get_conversation = AsyncMock(return_value=MagicMock(case_id="Q2026072747493"))
    agent_client = MagicMock()

    async def resume_stream(_session_id):
        yield {
            "type": "stage_update",
            "stage": "tool_call",
            "metadata": {
                "exec_id": "exec-resume-1",
                "tool_name": "qfk_system",
                "args": {"command": "lsof"},
                "status": "running",
            },
        }
        yield {
            "type": "stage_update",
            "stage": "tool_result",
            "metadata": {
                "exec_id": "exec-resume-1",
                "tool_name": "qfk_system",
                "args": {"command": "lsof"},
                "result": "9527",
                "status": "success",
            },
        }
        yield {"type": "done"}

    agent_client.resume_stream = resume_stream
    service = ConversationService(
        repository=repository,
        ai_registry=MagicMock(),
        kb_client=AsyncMock(),
        session_factory=MagicMock(),
        agent_client=agent_client,
    )
    service._record_tool_call = AsyncMock()

    chunks = [chunk async for chunk in service.resume_ops_agent_stream(conversation_id)]

    assert any("event:tool_call:" in chunk for chunk in chunks)
    assert any("event:tool_result:" in chunk for chunk in chunks)
    assert service._record_tool_call.await_count == 2
    assert service._record_tool_call.await_args_list[0].kwargs == {
        "conversation_id": conversation_id,
        "case_id": "Q2026072747493",
        "stage": "tool_call",
        "metadata": {
            "exec_id": "exec-resume-1",
            "tool_name": "qfk_system",
            "args": {"command": "lsof"},
            "status": "running",
        },
    }
