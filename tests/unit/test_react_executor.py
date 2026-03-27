"""
ReactExecutor 单元测试

覆盖：
  - risk_level=3（block）的工具被拒绝执行
  - MAX_STEPS 达到上限后输出提示，不无限循环
  - 工具调用流程（mock glm_client，模拟推理→工具调用→观察）
  - 工具审计日志确认被写入
"""

import os
import sys

# 确保 app 指向 conversation-service
_svc = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..",
                                     "backend", "conversation-service"))
if _svc not in sys.path:
    sys.path.insert(0, _svc)

from unittest.mock import AsyncMock

import pytest


@pytest.fixture
def mock_glm():
    return AsyncMock()


@pytest.fixture
def mock_tool_executor():
    m = AsyncMock()
    m.execute.return_value = {"alarms": [], "total": 0}
    return m


@pytest.fixture
def mock_confirm():
    m = AsyncMock()
    m.request_confirm.return_value = True
    return m


@pytest.fixture
def mock_audit():
    return AsyncMock()


@pytest.fixture
def mock_sse():
    return AsyncMock()


@pytest.fixture
def executor(mock_glm, mock_tool_executor, mock_confirm, mock_audit, mock_sse):
    from app.core.react_executor import ReactExecutor
    return ReactExecutor(
        glm_client=mock_glm,
        tool_executor=mock_tool_executor,
        confirm_service=mock_confirm,
        audit_service=mock_audit,
        sse_emitter=mock_sse,
    )


def _make_llm_response(content=None, tool_calls=None, finish_reason="stop"):
    """构造 LLMResponse"""
    from app.core.glm_client import LLMResponse
    return LLMResponse(
        content=content,
        finish_reason=finish_reason,
        tool_calls=tool_calls or [],
        usage={},
    )


def _make_tool_call(name="get_active_alerts", args=None, tc_id="call_001"):
    """构造 ToolCall"""
    from app.core.glm_client import ToolCall
    return ToolCall(id=tc_id, name=name, args=args or {})


class TestReActExecutorBasic:
    """基础功能测试"""

    @pytest.mark.asyncio
    async def test_text_reply_yields_content_directly(self, executor, mock_glm):
        """GLM 返回 finish_reason=stop（无工具调用）时，直接 yield 内容并结束"""
        mock_glm.chat.return_value = _make_llm_response(content="诊断完成，问题已定位。")

        from app.core.react_executor import AgentState
        state = AgentState(session_id="sid-001", messages=[{"role": "user", "content": "有告警吗？"}])
        chunks = []
        async for chunk in executor.run(state, system_prompt="你是专家"):
            chunks.append(chunk)

        assert "诊断完成，问题已定位。" in chunks
        assert mock_glm.chat.call_count == 1

    @pytest.mark.asyncio
    async def test_max_steps_limit_stops_execution(self, executor, mock_glm):
        """达到 MAX_STEPS 限制时，输出提示消息并停止"""
        from app.core.react_executor import MAX_STEPS, AgentState

        # 每次调用都返回工具调用（永不返回 stop），触发循环上限
        mock_glm.chat.return_value = _make_llm_response(
            tool_calls=[_make_tool_call()],
            finish_reason="tool_calls",
        )

        state = AgentState(session_id="sid-002", messages=[{"role": "user", "content": "分析"}])
        chunks = []
        async for chunk in executor.run(state, system_prompt="系统"):
            chunks.append(chunk)

        # 必须包含上限提示
        full_output = "".join(chunks)
        assert "上限" in full_output or "MAX" in full_output.upper()
        # 步骤数不超过 MAX_STEPS
        assert state.step_count == MAX_STEPS

    @pytest.mark.asyncio
    async def test_block_policy_tool_is_rejected(self, executor, mock_glm):
        """policy=block 的工具调用应被拒绝，不执行"""
        from app.core.react_executor import AgentState
        from app.core.tool_registry import TOOL_REGISTRY, ToolDefinition

        # 临时注册一个 block 工具
        TOOL_REGISTRY["dangerous_op"] = ToolDefinition(
            name="dangerous_op",
            description="危险操作",
            parameters={"type": "object", "properties": {}, "required": []},
            risk_level=3,
            policy="block",
            category="scp",
        )

        # 第一轮：GLM 调用 block 工具；第二轮：返回最终文字
        call_count = 0

        async def side_effect(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _make_llm_response(
                    tool_calls=[_make_tool_call(name="dangerous_op")],
                    finish_reason="tool_calls",
                )
            return _make_llm_response(content="操作已被阻止", finish_reason="stop")

        mock_glm.chat.side_effect = side_effect

        state = AgentState(session_id="sid-003", messages=[{"role": "user", "content": "执行危险操作"}])
        chunks = []
        async for chunk in executor.run(state, system_prompt="系统"):
            chunks.append(chunk)

        # tool_executor 不应被调用（block 工具不执行）
        executor.tool_executor.execute.assert_not_called()

        # 清理测试数据
        del TOOL_REGISTRY["dangerous_op"]

    @pytest.mark.asyncio
    async def test_audit_log_written_after_tool_call(self, executor, mock_glm, mock_audit):
        """工具调用完成后，audit_service.write 应被调用一次"""
        from app.core.react_executor import AgentState

        call_count = 0

        async def side_effect(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _make_llm_response(
                    tool_calls=[_make_tool_call("get_active_alerts", {})],
                    finish_reason="tool_calls",
                )
            return _make_llm_response(content="已查询告警列表。", finish_reason="stop")

        mock_glm.chat.side_effect = side_effect

        state = AgentState(session_id="sid-004", messages=[{"role": "user", "content": "查告警"}])
        async for _ in executor.run(state, system_prompt="系统"):
            pass

        # 审计日志应被写入一次（对应 get_active_alerts 工具调用）
        mock_audit.write.assert_called_once()
        call_kwargs = mock_audit.write.call_args.kwargs
        assert call_kwargs["tool_name"] == "get_active_alerts"
        assert call_kwargs["session_id"] == "sid-004"

    @pytest.mark.asyncio
    async def test_tool_result_added_to_message_history(self, executor, mock_glm):
        """工具调用结果应作为 tool 角色消息加入 messages"""
        from app.core.react_executor import AgentState

        call_count = 0

        async def side_effect(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _make_llm_response(
                    tool_calls=[_make_tool_call("get_active_alerts", {}, "call_x1")],
                    finish_reason="tool_calls",
                )
            return _make_llm_response(content="分析完毕", finish_reason="stop")

        mock_glm.chat.side_effect = side_effect

        state = AgentState(session_id="sid-005", messages=[{"role": "user", "content": "查告警"}])
        async for _ in executor.run(state, system_prompt="系统"):
            pass

        # 第二次调用的 messages 中应包含 role=tool 的工具结果
        second_call_msgs = mock_glm.chat.call_args_list[1].kwargs.get("messages", [])
        tool_msgs = [m for m in second_call_msgs if m.get("role") == "tool"]
        assert len(tool_msgs) == 1
        assert tool_msgs[0]["tool_call_id"] == "call_x1"
