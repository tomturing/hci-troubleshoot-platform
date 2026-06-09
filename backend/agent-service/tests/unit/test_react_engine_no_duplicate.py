"""
测试 ReactEngine 工具不重复执行

验证 T-AGT-05 修复：
- 工具执行器（ToolExecutor）在一次工具调用中只被调用一次
- 审计日志只写入一次
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from app.adapters.agents.htp.react_engine import ReactEngine
from app.adapters.agents.htp.tool_registry import TOOL_REGISTRY
from app.domain.agent_port import ToolResultEvent
from shared.clients import AIAssistantRegistry
from shared.clients.ai_client import InvokeResult, ToolCallRequest

# ─── Mock Helpers ─────────────────────────────────────────────────────────────


def _make_ai_registry_with_tool_call(tool_name: str = "get_active_alerts", tool_args: dict | None = None):
    """构建返回工具调用的 AI Registry mock"""
    if tool_args is None:
        tool_args = {}
    mock_client = MagicMock()

    # 第一次 invoke 返回工具调用
    tool_call = ToolCallRequest(
        id="call_123",
        name=tool_name,
        arguments=tool_args,
    )
    invoke_result = InvokeResult(content=None, tool_calls=[tool_call])

    # 第二次 invoke 返回文字回复（终止循环）
    final_result = InvokeResult(content="诊断完成", tool_calls=[])

    mock_client.invoke = AsyncMock(side_effect=[invoke_result, final_result])

    # chat_completion_stream 返回 AsyncGenerator
    async def fake_stream(messages, user_id, **kwargs):
        for chunk in ["诊断", "完成"]:
            yield chunk

    mock_client.chat_completion_stream = fake_stream

    mock_registry = MagicMock(spec=AIAssistantRegistry)
    mock_registry.get_client = MagicMock(return_value=mock_client)
    return mock_registry


def _make_ai_registry_with_single_tool_call():
    """构建只返回一次工具调用的 AI Registry（随后终止）"""
    mock_client = MagicMock()

    tool_call = ToolCallRequest(
        id="call_123",
        name="get_active_alerts",
        arguments={"cluster": "cluster1"},
    )
    invoke_result = InvokeResult(content=None, tool_calls=[tool_call])
    final_result = InvokeResult(content="诊断完成", tool_calls=[])

    mock_client.invoke = AsyncMock(side_effect=[invoke_result, final_result])

    async def fake_stream(messages, user_id, **kwargs):
        for chunk in ["诊断", "完成"]:
            yield chunk

    mock_client.chat_completion_stream = fake_stream

    mock_registry = MagicMock(spec=AIAssistantRegistry)
    mock_registry.get_client = MagicMock(return_value=mock_client)
    return mock_registry


def _make_ai_registry_with_error_then_final():
    """构建第一次工具调用失败，第二次返回文字的 mock"""
    mock_client = MagicMock()

    tool_call = ToolCallRequest(
        id="call_err",
        name="get_active_alerts",
        arguments={"cluster": "bad"},
    )
    invoke_result = InvokeResult(content=None, tool_calls=[tool_call])
    final_result = InvokeResult(content="诊断完成", tool_calls=[])

    mock_client.invoke = AsyncMock(side_effect=[invoke_result, final_result])

    async def fake_stream(messages, user_id, **kwargs):
        yield "诊断完成"

    mock_client.chat_completion_stream = fake_stream

    mock_registry = MagicMock(spec=AIAssistantRegistry)
    mock_registry.get_client = MagicMock(return_value=mock_client)
    return mock_registry


# ─── Test Fixtures ───────────────────────────────────────────────────────────


@pytest.fixture
def mock_tool_executor():
    """Mock 工具执行器"""
    executor = AsyncMock()
    executor.execute = AsyncMock(return_value={"alerts": ["alert1", "alert2"]})
    return executor


@pytest.fixture
def mock_audit_service():
    """Mock 审计服务"""
    audit = AsyncMock()
    audit.write = AsyncMock()
    return audit


# ─── 测试用例 ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tool_executed_only_once(mock_tool_executor, mock_audit_service):
    """
    验证工具执行器只被调用一次

    修复前：_execute_tool_call 内部调用一次，主循环 _get_tool_result 再调用一次
    修复后：只有 _execute_tool_call 调用一次
    """
    mock_registry = _make_ai_registry_with_single_tool_call()

    engine = ReactEngine(
        ai_registry=mock_registry,
        tool_registry=TOOL_REGISTRY,
        tool_executor=mock_tool_executor,
        confirm_service=None,
        audit_service=mock_audit_service,
    )

    events = []
    async for event in engine.execute(
        session_id="test-session",
        system_prompt="test",
        messages=[{"role": "user", "content": "测试"}],
        max_iterations=2,
    ):
        events.append(event)

    # 验证工具执行器只被调用一次
    assert mock_tool_executor.execute.call_count == 1, (
        f"工具执行器应只调用 1 次，实际调用 {mock_tool_executor.execute.call_count} 次"
    )

    # 验证审计日志写入（阶段一引入多状态审计链: proposed → committed，至少 1 次）
    assert mock_audit_service.write.call_count >= 1, (
        f"审计日志至少写入 1 次，实际 {mock_audit_service.write.call_count} 次"
    )


@pytest.mark.asyncio
async def test_tool_result_passed_to_main_loop(mock_tool_executor, mock_audit_service):
    """
    验证 ToolResultEvent 传递工具结果给主循环（不对外 yield）

    ToolResultEvent 是内部事件，用于将结果从 _execute_tool_call 传回主循环，
    不应该出现在外部事件流中。
    """
    mock_registry = _make_ai_registry_with_single_tool_call()

    engine = ReactEngine(
        ai_registry=mock_registry,
        tool_registry=TOOL_REGISTRY,
        tool_executor=mock_tool_executor,
        confirm_service=None,
        audit_service=mock_audit_service,
    )

    events = []
    async for event in engine.execute(
        session_id="test-session",
        system_prompt="test",
        messages=[{"role": "user", "content": "测试"}],
        max_iterations=2,
    ):
        events.append(event)

    # ToolResultEvent 不应该对外 yield（是内部事件）
    tool_result_events = [e for e in events if isinstance(e, ToolResultEvent)]
    assert len(tool_result_events) == 0, "ToolResultEvent 是内部事件，不应 yield 到外部"

    # 但工具结果应正确追加到消息历史（通过检查第二次 invoke 的 messages）
    mock_client = mock_registry.get_client.return_value
    second_call_args = mock_client.invoke.call_args_list[1]
    messages = second_call_args.kwargs.get("messages", [])
    tool_messages = [m for m in messages if m.get("role") == "tool"]
    assert len(tool_messages) == 1

    # 验证工具结果在消息内容中
    tool_content = tool_messages[0]["content"]
    assert "alert1" in str(tool_content) or "alerts" in str(tool_content)


@pytest.mark.asyncio
async def test_tool_result_added_to_message_history(mock_tool_executor, mock_audit_service):
    """
    验证工具结果正确追加到消息历史

    通过检查 LLM invoke 调用时的 messages 参数来验证
    """
    mock_registry = _make_ai_registry_with_single_tool_call()

    engine = ReactEngine(
        ai_registry=mock_registry,
        tool_registry=TOOL_REGISTRY,
        tool_executor=mock_tool_executor,
        confirm_service=None,
        audit_service=mock_audit_service,
    )

    events = []
    async for event in engine.execute(
        session_id="test-session",
        system_prompt="test",
        messages=[{"role": "user", "content": "测试"}],
        max_iterations=2,
    ):
        events.append(event)

    # invoke 应被调用两次（第一次工具调用，第二次文字终止）
    mock_client = mock_registry.get_client.return_value
    assert mock_client.invoke.call_count == 2

    # 检查第二次调用的 messages
    second_call_args = mock_client.invoke.call_args_list[1]
    messages = second_call_args.kwargs.get("messages", [])

    # 最后一条消息应该是 tool 角色消息
    tool_messages = [m for m in messages if m.get("role") == "tool"]
    assert len(tool_messages) == 1, "应有 1 条 tool 消息"
    assert tool_messages[0]["tool_call_id"] == "call_123"

    # 验证工具结果在消息内容中
    tool_content = tool_messages[0]["content"]
    assert "alert1" in str(tool_content) or "alerts" in str(tool_content)


@pytest.mark.asyncio
async def test_tool_execution_error_handled_once():
    """
    验证工具执行错误时也只调用一次，错误信息正确传递
    """
    mock_registry = _make_ai_registry_with_error_then_final()

    # 工具执行抛出异常
    mock_executor = AsyncMock()
    mock_executor.execute = AsyncMock(side_effect=RuntimeError("工具执行失败"))

    mock_audit = AsyncMock()
    mock_audit.write = AsyncMock()

    engine = ReactEngine(
        ai_registry=mock_registry,
        tool_registry=TOOL_REGISTRY,
        tool_executor=mock_executor,
        confirm_service=None,
        audit_service=mock_audit,
    )

    events = []
    async for event in engine.execute(
        session_id="test-session",
        system_prompt="test",
        messages=[{"role": "user", "content": "测试"}],
        max_iterations=2,
    ):
        events.append(event)

    # 工具执行器只调用一次（即使失败）
    assert mock_executor.execute.call_count == 1

    # 审计日志记录错误（阶段一引入多状态审计链: proposed → failed，至少 1 次）
    assert mock_audit.write.call_count >= 1
    # 验证至少有一次审计包含错误信息
    all_errors = [c.kwargs.get("error") for c in mock_audit.write.call_args_list]
    assert any(e is not None for e in all_errors), "应有至少一次审计写入包含错误信息"

    # ToolResultEvent 不对外 yield（是内部事件）
    tool_result_events = [e for e in events if isinstance(e, ToolResultEvent)]
    assert len(tool_result_events) == 0

    # 验证错误信息传递到消息历史
    mock_client = mock_registry.get_client.return_value
    second_call_args = mock_client.invoke.call_args_list[1]
    messages = second_call_args.kwargs.get("messages", [])
    tool_messages = [m for m in messages if m.get("role") == "tool"]
    assert len(tool_messages) == 1

    # 工具消息应包含错误信息
    tool_content = tool_messages[0]["content"]
    assert "失败" in str(tool_content) or "error" in str(tool_content).lower()


@pytest.mark.asyncio
async def test_no_tool_result_event_for_text_response(mock_tool_executor, mock_audit_service):
    """
    验证当 LLM 直接返回文字时，不会 yield ToolResultEvent
    """
    mock_client = MagicMock()

    # 直接返回文字回复（无工具调用）
    invoke_result = InvokeResult(content="直接回复", tool_calls=[])
    mock_client.invoke = AsyncMock(return_value=invoke_result)

    async def fake_stream(messages, user_id, **kwargs):
        yield "直接回复"

    mock_client.chat_completion_stream = fake_stream

    mock_registry = MagicMock(spec=AIAssistantRegistry)
    mock_registry.get_client = MagicMock(return_value=mock_client)

    engine = ReactEngine(
        ai_registry=mock_registry,
        tool_registry=TOOL_REGISTRY,
        tool_executor=mock_tool_executor,
        confirm_service=None,
        audit_service=mock_audit_service,
    )

    events = []
    async for event in engine.execute(
        session_id="test-session",
        system_prompt="test",
        messages=[{"role": "user", "content": "测试"}],
        max_iterations=1,
    ):
        events.append(event)

    # 无 ToolResultEvent
    tool_result_events = [e for e in events if isinstance(e, ToolResultEvent)]
    assert len(tool_result_events) == 0

    # 工具执行器未被调用
    assert mock_tool_executor.execute.call_count == 0

    # 无审计日志
    assert mock_audit_service.write.call_count == 0


@pytest.mark.asyncio
async def test_react_engine_sop_mode_filtering(mock_tool_executor, mock_audit_service):
    """
    验证 sop_mode=True 时包含 category='sop' 的工具，sop_mode=False 时过滤掉
    """
    mock_client = MagicMock()
    invoke_result = InvokeResult(content="直接回复", tool_calls=[])
    mock_client.invoke = AsyncMock(return_value=invoke_result)

    async def fake_stream(messages, user_id, **kwargs):
        yield "直接回复"

    mock_client.chat_completion_stream = fake_stream

    mock_registry = MagicMock(spec=AIAssistantRegistry)
    mock_registry.get_client = MagicMock(return_value=mock_client)

    engine = ReactEngine(
        ai_registry=mock_registry,
        tool_registry=TOOL_REGISTRY,
        tool_executor=mock_tool_executor,
        confirm_service=None,
        audit_service=mock_audit_service,
    )

    # 1. 测试 sop_mode=False (默认情况)
    async for _ in engine.execute(
        session_id="test-session-1",
        system_prompt="test",
        messages=[{"role": "user", "content": "测试"}],
        max_iterations=1,
        sop_mode=False,
    ):
        pass

    # 检查传给 invoke 的 tools，应不包含 sop 分类的工具
    first_call_args = mock_client.invoke.call_args_list[0]
    tools_passed = first_call_args.kwargs.get("tools", [])
    sop_tools = [t for t in tools_passed if t["function"]["name"] in ("get_sop_node", "sop_advance")]
    assert len(sop_tools) == 0, f"非 SOP 模式不应包含 SOP 工具，但发现了：{sop_tools}"

    # 2. 测试 sop_mode=True
    mock_client.invoke.reset_mock()
    async for _ in engine.execute(
        session_id="test-session-2",
        system_prompt="test",
        messages=[{"role": "user", "content": "测试"}],
        max_iterations=1,
        sop_mode=True,
    ):
        pass

    # 检查传给 invoke 的 tools，应包含 sop 分类的工具
    second_call_args = mock_client.invoke.call_args_list[0]
    tools_passed_sop = second_call_args.kwargs.get("tools", [])
    sop_tools_present = [t for t in tools_passed_sop if t["function"]["name"] in ("get_sop_node", "sop_advance")]
    assert len(sop_tools_present) > 0, "SOP 模式必须包含 SOP 导航工具"
