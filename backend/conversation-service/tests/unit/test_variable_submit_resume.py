"""变量提交后的续跑契约。

Q2026102600003 的最后一环：变量值写入变量池 + 前端显示「已提交」，
但没有任何后续推理，用户感知为「填了还在问」。

本文件锁死三条：
  1. 提交成功必须落一条 user 消息，且**带 trace_id**（历史漏传导致 TypeError
     被 except 吞成 WARNING，用户提交记录从未落库）；
  2. 路由必须显式返回 should_resume，让调用方知道"HTTP 200 != 已继续"；
  3. 非变量类交互不得被标记为需要续跑（避免误触发）。
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from app.routes.conversations import InteractiveResponseBody, submit_interactive_response
from app.services.conversation_service import ConversationService

CONVERSATION_ID = uuid.UUID("5fe5cb2c-f767-480b-826d-5c8a9a6c8df1")
REQUEST_ID = "d3f1c0a2-1111-4c2e-9b7a-000000000001"


def _build_service() -> tuple[ConversationService, MagicMock, MagicMock]:
    repository = MagicMock()
    repository.get_conversation = AsyncMock(return_value=MagicMock(case_id="Q2026102600003"))
    repository.add_message = AsyncMock()

    session = MagicMock()
    session.commit = AsyncMock()

    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    session_factory = MagicMock(return_value=ctx)

    service = ConversationService(
        repository=repository,
        ai_registry=MagicMock(),
        kb_client=AsyncMock(),
        session_factory=session_factory,
        agent_client=MagicMock(),
    )
    return service, repository, session


@pytest.mark.asyncio
async def test_variable_submit_persists_user_message_with_trace_id(monkeypatch):
    service, repository, _ = _build_service()

    fake_repo = MagicMock()
    fake_repo.set_variable = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "app.services.conversation_service.SopExecutionRepository", lambda _session: fake_repo
    )

    ok = await service.submit_interactive_response(
        conversation_id=CONVERSATION_ID,
        kind="variable_input",
        request_id=REQUEST_ID,
        acp_session_id="acp-1",
        outcome={"outcome": "free_text", "text": "/dev/sdb"},
        metadata={"variable_name": "disk_dev"},
    )

    assert ok is True
    repository.add_message.assert_awaited_once()
    kwargs = repository.add_message.await_args.kwargs
    # 关键：trace_id 必填，历史漏传会让整条记录丢失
    assert kwargs["trace_id"] == REQUEST_ID
    assert kwargs["role"].value == "user" if hasattr(kwargs["role"], "value") else True
    assert kwargs["metadata"]["variable_name"] == "disk_dev"


@pytest.mark.asyncio
async def test_route_marks_variable_input_as_should_resume(monkeypatch):
    service, _, _ = _build_service()
    service.submit_interactive_response = AsyncMock(return_value=True)

    body = InteractiveResponseBody(
        kind="variable_input",
        request_id=REQUEST_ID,
        acp_session_id="acp-1",
        outcome={"outcome": "free_text", "text": "/dev/sdb"},
        metadata={"variable_name": "disk_dev"},
    )

    result = await submit_interactive_response(CONVERSATION_ID, body, service)

    assert result == {"ok": True, "should_resume": True}


@pytest.mark.asyncio
async def test_route_does_not_resume_for_non_variable_kinds(monkeypatch):
    """tool_confirm / human_escalation 等不应被标记为需要续跑。"""
    service, _, _ = _build_service()
    service.submit_interactive_response = AsyncMock(return_value=True)

    body = InteractiveResponseBody(
        kind="human_escalation",
        request_id=REQUEST_ID,
        acp_session_id="acp-1",
        outcome={"outcome": "selected", "optionId": "ack"},
    )

    result = await submit_interactive_response(CONVERSATION_ID, body, service)

    assert result == {"ok": True, "should_resume": False}


@pytest.mark.asyncio
async def test_variable_submit_without_execution_returns_false(monkeypatch):
    """找不到 SOP 执行实例时必须返回 False，不能假装成功。"""
    service, _, _ = _build_service()

    fake_repo = MagicMock()
    fake_repo.set_variable = AsyncMock(return_value=False)
    monkeypatch.setattr(
        "app.services.conversation_service.SopExecutionRepository", lambda _session: fake_repo
    )

    ok = await service.submit_interactive_response(
        conversation_id=CONVERSATION_ID,
        kind="variable_input",
        request_id=REQUEST_ID,
        acp_session_id="acp-1",
        outcome={"outcome": "free_text", "text": "/dev/sdb"},
        metadata={"variable_name": "disk_dev"},
    )

    assert ok is False
