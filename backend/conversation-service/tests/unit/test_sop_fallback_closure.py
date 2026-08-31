"""SOP 主动退出并切换 KBD 的生产闭环测试。"""

import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.models.sop_execution import STATUS_ABORTED, STATUS_ACTIVE, STATUS_COMPLETED
from app.routes import conversations as conversation_routes
from app.services.conversation_service import ConversationService
from fastapi import BackgroundTasks
from shared.models.schemas import MessageCreate


class AsyncContext:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *_args):
        return False


def scalar_result(value):
    result = MagicMock()
    result.scalar_one_or_none.return_value = value
    return result


def make_service(*, session=None, agent_client=None):
    repository = MagicMock()
    repository.get_conversation = AsyncMock()
    repository.get_messages = AsyncMock(return_value=[])
    repository.add_message = AsyncMock(return_value=None)
    return ConversationService(
        repository=repository,
        ai_registry=MagicMock(),
        session_factory=(MagicMock(return_value=AsyncContext(session)) if session else None),
        agent_client=agent_client,
    )


@pytest.mark.asyncio
async def test_sop_stream_end_emits_persisted_fallback_prompt():
    conversation_id = uuid.uuid4()
    conv = MagicMock(trace_id=None, diagnostic_stage="S1", category_id="虚拟机-003")

    class AgentClient:
        async def stream(self, **_kwargs):
            yield {"type": "stage_update", "stage": "sop_reasoning", "metadata": {"sop_document_id": 3}}
            yield {"type": "text_chunk", "content": "当前 SOP 无法继续获取现场信息。"}
            yield {"type": "done"}

    service = make_service(agent_client=AgentClient())
    service.repository.get_conversation = AsyncMock(return_value=conv)
    service.repository.add_message = AsyncMock(return_value=None)
    service._update_sop_usage = AsyncMock()
    fallback_event = {
        "requestId": "sop-fallback-1",
        "acpSessionId": str(conversation_id),
        "kind": "sop_fallback",
        "title": "选择后续排查方式",
        "prompt": "请选择",
        "options": [{"optionId": "switch_kbd", "name": "SOP 未解决，切换 KBD"}],
        "customInput": False,
        "metadata": {},
    }
    service.send_sop_fallback_options = AsyncMock(return_value=fallback_event)

    chunks = [
        chunk
        async for chunk in service.send_message_stream_only(
            conversation_id=conversation_id,
            case_id="Q2026083113575",
            content="虚拟机开机失败",
            assistant_type="htp-agent",
        )
    ]

    service.send_sop_fallback_options.assert_awaited_once_with(
        conversation_id=conversation_id,
        stage="S1",
    )
    encoded = next(chunk for chunk in chunks if chunk.startswith("\x00event:interactive_request:"))
    payload = json.loads(encoded.removeprefix("\x00event:interactive_request:").removesuffix("\x00"))
    assert payload["kind"] == "sop_fallback"


@pytest.mark.asyncio
async def test_existing_blocking_interaction_suppresses_fallback_prompt():
    conversation_id = uuid.uuid4()
    conv = MagicMock(trace_id=None, diagnostic_stage="S1", category_id="虚拟机-003")

    class AgentClient:
        async def stream(self, **_kwargs):
            yield {"type": "stage_update", "stage": "sop_reasoning", "metadata": {"sop_document_id": 3}}
            yield {
                "type": "interactive_request",
                "request_id": "variable-1",
                "acp_session_id": str(conversation_id),
                "kind": "variable_input",
                "title": "补充变量",
                "prompt": "请输入 VM ID",
                "options": [],
                "custom_input": True,
                "metadata": {"variable_name": "vm_id"},
            }
            yield {"type": "done"}

    service = make_service(agent_client=AgentClient())
    service.repository.get_conversation = AsyncMock(return_value=conv)
    service.repository.add_message = AsyncMock(return_value=None)
    service._update_sop_usage = AsyncMock()
    service.send_sop_fallback_options = AsyncMock()

    _ = [
        chunk
        async for chunk in service.send_message_stream_only(
            conversation_id=conversation_id,
            case_id="Q2026083113575",
            content="虚拟机开机失败",
            assistant_type="htp-agent",
        )
    ]

    service.send_sop_fallback_options.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_sop_fallback_options_persists_one_time_snapshot():
    conversation_id = uuid.uuid4()
    conv = MagicMock(pending_confirm=None, pending_resolution=None)
    sop = MagicMock(sop_document_id=3, status=STATUS_COMPLETED)
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[scalar_result(conv), scalar_result(sop)])
    service = make_service(session=session)

    event = await service.send_sop_fallback_options(conversation_id, "S4")

    assert event is not None
    assert event["kind"] == "sop_fallback"
    assert [item["optionId"] for item in event["options"]] == ["continue_sop", "switch_kbd"]
    assert conv.pending_resolution["request_id"] == event["requestId"]
    assert conv.pending_resolution["sop_status"] == STATUS_COMPLETED
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_switch_kbd_atomically_aborts_sop_and_archives_diagnostics():
    conversation_id = uuid.uuid4()
    request_id = "sop-fallback-valid"
    conv = MagicMock(
        case_id="Q2026083113575",
        diagnostic_stage="S3",
        pending_resolution={
            "kind": "sop_fallback",
            "request_id": request_id,
            "sop_document_id": 3,
        },
    )
    sop = MagicMock(sop_document_id=3, status=STATUS_ACTIVE, pending_variable_name="vm_id")
    archived = MagicMock(rowcount=4)
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[scalar_result(conv), scalar_result(sop), archived])
    session.add = MagicMock()
    service = make_service(session=session)

    result = await service.handle_sop_fallback_choice(
        conversation_id=conversation_id,
        request_id=request_id,
        choice="switch_kbd",
        content="SOP 未解决，切换 KBD",
        metadata={"kind": "interactive_response", "interactiveKind": "sop_fallback"},
        trace_id="a" * 32,
    )

    assert sop.status == STATUS_ABORTED
    assert sop.pending_variable_name is None
    assert conv.pending_resolution is None
    assert conv.diagnostic_stage == "S1"
    assert result["archived_count"] == 4
    session.add.assert_called_once()
    session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_continue_sop_clears_snapshot_without_aborting():
    conversation_id = uuid.uuid4()
    request_id = "sop-fallback-continue"
    conv = MagicMock(
        case_id="Q2026083113575",
        diagnostic_stage="S2",
        pending_resolution={
            "kind": "sop_fallback",
            "request_id": request_id,
            "sop_document_id": 3,
        },
    )
    sop = MagicMock(sop_document_id=3, status=STATUS_ACTIVE)
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[scalar_result(conv), scalar_result(sop)])
    session.add = MagicMock()
    service = make_service(session=session)

    await service.handle_sop_fallback_choice(
        conversation_id=conversation_id,
        request_id=request_id,
        choice="continue_sop",
        content="继续按当前 SOP 排查",
        metadata={"kind": "interactive_response", "interactiveKind": "sop_fallback"},
        trace_id="b" * 32,
    )

    assert sop.status == STATUS_ACTIVE
    assert conv.diagnostic_stage == "S2"
    assert conv.pending_resolution is None
    assert session.execute.await_count == 2


@pytest.mark.asyncio
async def test_replayed_or_forged_request_is_rejected():
    conversation_id = uuid.uuid4()
    conv = MagicMock(case_id="Q2026083113575", pending_resolution=None)
    session = AsyncMock()
    session.execute = AsyncMock(return_value=scalar_result(conv))
    service = make_service(session=session)

    with pytest.raises(ValueError, match="没有待处理"):
        await service.handle_sop_fallback_choice(
            conversation_id=conversation_id,
            request_id="replayed",
            choice="switch_kbd",
            content="伪造选择",
            metadata={"kind": "interactive_response", "interactiveKind": "sop_fallback"},
            trace_id="c" * 32,
        )

    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_stale_snapshot_cannot_abort_a_different_sop():
    conversation_id = uuid.uuid4()
    request_id = "sop-fallback-stale"
    conv = MagicMock(
        pending_resolution={
            "kind": "sop_fallback",
            "request_id": request_id,
            "sop_document_id": 3,
        }
    )
    sop = MagicMock(sop_document_id=4, status=STATUS_ACTIVE)
    session = AsyncMock()
    session.execute = AsyncMock(side_effect=[scalar_result(conv), scalar_result(sop)])
    service = make_service(session=session)

    with pytest.raises(ValueError, match="快照不一致"):
        await service.handle_sop_fallback_choice(
            conversation_id=conversation_id,
            request_id=request_id,
            choice="switch_kbd",
            content="SOP 未解决，切换 KBD",
            metadata={"kind": "interactive_response", "interactiveKind": "sop_fallback"},
            trace_id="d" * 32,
        )

    assert sop.status == STATUS_ACTIVE
    assert conv.pending_resolution is not None
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_rejected_choice_uses_bounded_metric_label():
    conversation_id = uuid.uuid4()
    agent_client = MagicMock()
    service = make_service(agent_client=agent_client)
    service.repository.get_conversation = AsyncMock(return_value=MagicMock(trace_id=None))
    service.handle_sop_fallback_choice = AsyncMock(side_effect=ValueError("选择值无效"))

    with patch("app.services.conversation_service.SOP_FALLBACK_DECISIONS_TOTAL") as metric:
        chunks = [
            chunk
            async for chunk in service.send_message_stream_only(
                conversation_id=conversation_id,
                case_id="Q2026083113575",
                content="伪造选择",
                assistant_type="htp-agent",
                metadata={
                    "kind": "interactive_response",
                    "interactiveKind": "sop_fallback",
                    "selectedOptionId": "attacker-controlled-label",
                    "sourceRequestId": "forged",
                },
            )
        ]

    metric.labels.assert_called_once_with(choice="invalid", status="rejected")
    assert "选择值无效" in "".join(chunks)
    agent_client.stream.assert_not_called()


@pytest.mark.asyncio
async def test_route_persists_reply_before_sop_fallback_card():
    conversation_id = uuid.uuid4()
    fallback_event = {
        "requestId": "sop-fallback-order",
        "acpSessionId": str(conversation_id),
        "kind": "sop_fallback",
        "title": "选择后续排查方式",
        "prompt": "请选择",
        "options": [],
        "customInput": False,
        "metadata": {},
    }

    async def stream(**_kwargs):
        yield "SOP 本轮说明"
        yield f"\x00event:interactive_request:{json.dumps(fallback_event, ensure_ascii=False)}\x00"

    service = MagicMock()
    service.send_message_stream_only = stream
    request = MagicMock()
    request.headers = {}
    request.app.state.sse_pusher = None
    background_tasks = BackgroundTasks()
    message = MessageCreate(
        case_id="Q2026083113575",
        role="user",
        content="继续排查",
        assistant_type="htp-agent",
    )

    with patch(
        "app.routes.conversations.verify_conversation_ownership",
        new=AsyncMock(),
    ):
        response = await conversation_routes.send_message(
            conversation_id=conversation_id,
            message=message,
            background_tasks=background_tasks,
            request=request,
            service=service,
        )
        _ = [chunk async for chunk in response.body_iterator]

    assert [task.func for task in background_tasks.tasks] == [
        service.save_assistant_message,
        service.save_interactive_request_message,
    ]
    assert background_tasks.tasks[1].kwargs["event"] == fallback_event
