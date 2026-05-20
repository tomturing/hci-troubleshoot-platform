"""
AgentPort Protocol 和事件类型单元测试
"""

import pytest

from app.core.agent_port import (
    AgentEscalation,
    AgentInteractiveRequest,
    AgentStageUpdate,
    AgentTextChunk,
    AgentUnavailableError,
)


class TestAgentTextChunk:
    """AgentTextChunk 测试"""

    def test_create_text_chunk(self):
        """测试创建文本块"""
        chunk = AgentTextChunk(content="你好，我来帮你解决问题")
        assert chunk.content == "你好，我来帮你解决问题"

    def test_text_chunk_immutable(self):
        """测试文本块不可变"""
        chunk = AgentTextChunk(content="test")
        with pytest.raises(AttributeError):
            chunk.content = "changed"  # type: ignore

    def test_text_chunk_equality(self):
        """测试文本块相等性"""
        chunk1 = AgentTextChunk(content="same")
        chunk2 = AgentTextChunk(content="same")
        chunk3 = AgentTextChunk(content="different")
        assert chunk1 == chunk2
        assert chunk1 != chunk3


class TestAgentStageUpdate:
    """AgentStageUpdate 测试"""

    def test_create_stage_update(self):
        """测试创建阶段更新"""
        update = AgentStageUpdate(stage="S1")
        assert update.stage == "S1"
        assert update.metadata == {}

    def test_stage_update_with_metadata(self):
        """测试带元数据的阶段更新"""
        update = AgentStageUpdate(
            stage="S2",
            metadata={"confidence": 0.9, "trigger": "llm_output"}
        )
        assert update.stage == "S2"
        assert update.metadata["confidence"] == 0.9
        assert update.metadata["trigger"] == "llm_output"

    def test_stage_update_immutable(self):
        """测试阶段更新不可变"""
        update = AgentStageUpdate(stage="S1")
        with pytest.raises(AttributeError):
            update.stage = "S2"  # type: ignore


class TestAgentEscalation:
    """AgentEscalation 测试"""

    def test_create_escalation(self):
        """测试创建升级请求"""
        escalation = AgentEscalation(reason="问题超出处理范围")
        assert escalation.reason == "问题超出处理范围"
        assert escalation.context == {}

    def test_escalation_with_context(self):
        """测试带上下文的升级请求"""
        escalation = AgentEscalation(
            reason="需要人工介入",
            context={"case_id": "CASE001", "attempts": 3}
        )
        assert escalation.reason == "需要人工介入"
        assert escalation.context["case_id"] == "CASE001"


class TestAgentInteractiveRequest:
    """AgentInteractiveRequest 测试"""

    def test_create_interactive_request(self):
        """测试创建交互请求"""
        request = AgentInteractiveRequest(
            request_id="req-001",
            acp_session_id="session-001",
            kind="info_request",
            title="请提供VM ID",
            prompt="请告诉我你的虚拟机ID以便进一步排查"
        )
        assert request.request_id == "req-001"
        assert request.acp_session_id == "session-001"
        assert request.kind == "info_request"
        assert request.title == "请提供VM ID"
        assert request.prompt == "请告诉我你的虚拟机ID以便进一步排查"
        assert request.options == []
        assert request.custom_input is True
        assert request.metadata == {}

    def test_interactive_request_with_options(self):
        """测试带选项的交互请求"""
        options = [
            {"optionId": "yes", "name": "是"},
            {"optionId": "no", "name": "否"}
        ]
        request = AgentInteractiveRequest(
            request_id="req-002",
            acp_session_id="session-001",
            kind="sop_step",
            title="确认操作",
            prompt="请确认是否执行此操作",
            options=options,
            custom_input=False
        )
        assert len(request.options) == 2
        assert request.options[0]["optionId"] == "yes"
        assert request.custom_input is False


class TestAgentUnavailableError:
    """AgentUnavailableError 测试"""

    def test_create_error(self):
        """测试创建错误"""
        error = AgentUnavailableError(agent_name="ops-agent", reason="连接超时")
        assert error.agent_name == "ops-agent"
        assert error.reason == "连接超时"
        assert "ops-agent" in str(error)
        assert "连接超时" in str(error)

    def test_error_without_reason(self):
        """测试不带原因的错误"""
        error = AgentUnavailableError(agent_name="htp")
        assert error.agent_name == "htp"
        assert error.reason == ""
