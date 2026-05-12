"""
resume_ops_agent_stream 单元测试

覆盖以下场景：
1. BrainTextChunk → 产出文本内容
2. BrainInteractiveRequest → 产出 \\x00event:interactive_request:...\\x00 内部格式并落库
3. BrainStageUpdate → 产出 \\x00event:stage_change:...\\x00 内部格式
4. _conv=None 时 BrainInteractiveRequest 不触发落库（避免 case_id='' 脏数据）
"""

import asyncio
import os
import sys
import uuid
from typing import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock

import pytest

# 多服务共享 app/ 命名空间，仅在 app 指向错误服务时清除重载
_svc = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_expect = os.path.normpath(os.path.join(_svc, "app"))
_actual = os.path.normpath(getattr(sys.modules.get("app"), "__path__", [""])[0]) if "app" in sys.modules else ""
if _expect != _actual:
    for _k in list(sys.modules):
        if _k == "app" or _k.startswith("app."):
            del sys.modules[_k]
    if _svc in sys.path:
        sys.path.remove(_svc)
    sys.path.insert(0, _svc)

from app.core.brain_port import (
    BrainInteractiveRequest,
    BrainStageUpdate,
    BrainTextChunk,
)
from app.models.conversation import Conversation
from app.services.conversation_service import ConversationService


# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def mock_repo():
    m = AsyncMock()
    m.add_message = AsyncMock()
    return m


@pytest.fixture
def mock_registry():
    return MagicMock()


@pytest.fixture
def mock_scheduler():
    m = AsyncMock()
    m.allocate_pod.return_value = "test-pod-001"
    m.wait_for_endpoint.return_value = "http://localhost:9999"
    return m


@pytest.fixture
def service(mock_repo, mock_registry, mock_scheduler):
    return ConversationService(mock_repo, mock_registry, mock_scheduler)


def _make_ops_adapter(events: list) -> MagicMock:
    """构造返回指定事件序列的 OpsAgentBrainAdapter mock。"""

    async def _fake_resume(session_id: str) -> AsyncGenerator:
        for ev in events:
            yield ev

    adapter = MagicMock()
    adapter.resume_event_stream = _fake_resume
    return adapter


def _make_ir_event() -> BrainInteractiveRequest:
    return BrainInteractiveRequest(
        request_id="req-001",
        acp_session_id="sess-001",
        kind="info_request",
        title="确认信息",
        prompt="网络是否正常？",
        options=[{"optionId": "1", "name": "是"}, {"optionId": "2", "name": "否"}],
        custom_input=True,
        metadata={"context": "测试场景"},
    )


# ── Tests ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestResumeOpsAgentStreamOutput:
    """验证 resume_ops_agent_stream SSE 输出格式"""

    async def _run(self, service, conv_id, events, conv=None, repo=None):
        """搭建 mock 并收集 resume_ops_agent_stream 的输出片段列表。"""
        ops_adapter = _make_ops_adapter(events)
        mock_router = MagicMock()
        mock_router.get_ops_agent_adapter.return_value = ops_adapter
        service._brain_router = mock_router

        if repo is not None:
            mock_repo = repo
        else:
            mock_repo = service.repository
        mock_repo.get_conversation = AsyncMock(return_value=conv)
        mock_repo.add_message = AsyncMock()
        service.session_factory = None

        chunks = []
        async for chunk in service.resume_ops_agent_stream(conv_id):
            chunks.append(chunk)
        # 等待后台 create_task 完成
        await asyncio.sleep(0)
        return chunks

    async def test_text_chunk_yields_content(self, service, mock_repo):
        """BrainTextChunk → 产出文本内容字符串"""
        conv_id = uuid.uuid4()
        conv = Conversation(conversation_id=conv_id, case_id="Q001")
        events = [BrainTextChunk(content="hello"), BrainTextChunk(content=" world")]

        chunks = await self._run(service, conv_id, events, conv=conv, repo=mock_repo)

        assert chunks == ["hello", " world"]

    async def test_stage_change_yields_internal_marker(self, service, mock_repo):
        """BrainStageUpdate → 产出 \\x00event:stage_change:S3\\x00 格式"""
        conv_id = uuid.uuid4()
        conv = Conversation(conversation_id=conv_id, case_id="Q001")
        events = [BrainStageUpdate(stage="S3")]

        chunks = await self._run(service, conv_id, events, conv=conv, repo=mock_repo)

        assert len(chunks) == 1
        assert chunks[0] == "\x00event:stage_change:S3\x00"

    async def test_interactive_request_yields_marker(self, service, mock_repo):
        """BrainInteractiveRequest → 产出含 event:interactive_request 的内部标记"""
        conv_id = uuid.uuid4()
        conv = Conversation(conversation_id=conv_id, case_id="Q001")
        ir_event = _make_ir_event()

        chunks = await self._run(service, conv_id, [ir_event], conv=conv, repo=mock_repo)

        assert len(chunks) == 1
        assert chunks[0].startswith("\x00event:interactive_request:")
        assert "req-001" in chunks[0]
        assert chunks[0].endswith("\x00")

    async def test_interactive_request_saves_to_db_when_conv_exists(self, service, mock_repo):
        """BrainInteractiveRequest + conv 存在 → 触发落库（case_id 非空）"""
        conv_id = uuid.uuid4()
        conv = Conversation(conversation_id=conv_id, case_id="Q001")
        ir_event = _make_ir_event()

        await self._run(service, conv_id, [ir_event], conv=conv, repo=mock_repo)

        # add_message 应被调用一次（_save_message_bg 走 repository 路径）
        assert mock_repo.add_message.called, "_save_message_bg 应在 conv 存在时落库"
        call_kwargs = mock_repo.add_message.call_args.kwargs
        assert call_kwargs.get("case_id") == "Q001", "落库 case_id 应等于会话的 case_id"
        assert call_kwargs.get("metadata", {}).get("kind") == "interactive_request"

    async def test_interactive_request_skips_db_when_conv_not_found(self, service, mock_repo):
        """_conv=None 时 BrainInteractiveRequest 不触发落库，避免 case_id='' 脏数据"""
        conv_id = uuid.uuid4()
        ir_event = _make_ir_event()

        await self._run(service, conv_id, [ir_event], conv=None, repo=mock_repo)

        # add_message 不应被调用（_case_id is None 时跳过 create_task）
        mock_repo.add_message.assert_not_called()

    async def test_empty_text_chunk_not_yielded(self, service, mock_repo):
        """BrainTextChunk.content 为空字符串时不产出"""
        conv_id = uuid.uuid4()
        conv = Conversation(conversation_id=conv_id, case_id="Q001")
        events = [BrainTextChunk(content=""), BrainTextChunk(content="ok")]

        chunks = await self._run(service, conv_id, events, conv=conv, repo=mock_repo)

        assert chunks == ["ok"], "空 content 的 BrainTextChunk 不应产出"

    async def test_no_ops_adapter_yields_nothing(self, service, mock_repo):
        """_brain_router.get_ops_agent_adapter() 返回 None 时不产出任何内容"""
        conv_id = uuid.uuid4()
        mock_router = MagicMock()
        mock_router.get_ops_agent_adapter.return_value = None
        service._brain_router = mock_router

        chunks = []
        async for chunk in service.resume_ops_agent_stream(conv_id):
            chunks.append(chunk)

        assert chunks == []
