"""
ReactEngine 工具语义校验测试。
"""

import copy

import pytest
from app.adapters.agents.htp.react_engine import ReactEngine
from app.domain.agent_port import AgentTextChunk, ToolResultEvent
from app.tools.base_tool import ToolDefinition


class _FakeExecutor:
    def __init__(self):
        self.called = False

    async def execute(self, tool_name: str, args: dict, **kwargs):
        self.called = True
        return {"ok": True}


@pytest.mark.asyncio
async def test_bash_exec_missing_container_does_not_call_executor():
    executor = _FakeExecutor()
    engine = ReactEngine(
        ai_registry=None,
        tool_registry={
            "bash_exec": ToolDefinition(
                name="bash_exec",
                description="执行 Bash 命令",
                parameters={
                    "type": "object",
                    "properties": {
                        "command": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    "required": ["command", "reason"],
                },
                risk_level=1,
                policy="auto",
                category="acli",
            )
        },
        tool_executor=executor,
    )

    events = [
        event
        async for event in engine._execute_tool_call(
            tool_call={"id": "call-1", "name": "bash_exec", "args": {"command": "ps aux", "reason": "检查进程"}},
            session_id="session-1",
            step=1,
        )
    ]

    assert executor.called is False
    result_events = [event for event in events if isinstance(event, ToolResultEvent)]
    assert result_events
    assert "BASH_CONTAINER_REQUIRED" in str(result_events[0].result)


@pytest.mark.asyncio
async def test_semantic_validation_feedback_is_failed_observation_for_llm():
    class _ToolCall:
        id = "call-1"
        name = "bash_exec"
        arguments = {"command": "ps aux", "reason": "检查进程"}

    executor = _FakeExecutor()
    captured_messages = []

    class _FakeAiClient:
        def __init__(self):
            self.invoke_count = 0

        async def invoke(self, *, messages, **kwargs):
            self.invoke_count += 1
            captured_messages.append(copy.deepcopy(messages))
            if self.invoke_count == 1:
                return type("InvokeResult", (), {"content": None, "tool_calls": [_ToolCall()]})()
            return type("InvokeResult", (), {"content": "已重新规划", "tool_calls": []})()

        async def chat_completion_stream(self, **kwargs):
            yield "已重新规划"

    class _Registry:
        def __init__(self):
            self.client = _FakeAiClient()

        def get_client(self, assistant_type):
            return self.client

    engine = ReactEngine(
        ai_registry=_Registry(),
        tool_registry={
            "bash_exec": ToolDefinition(
                name="bash_exec",
                description="执行 Bash 命令",
                parameters={
                    "type": "object",
                    "properties": {
                        "command": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    "required": ["command", "reason"],
                },
                risk_level=1,
                policy="auto",
                category="acli",
            )
        },
        tool_executor=executor,
    )

    events = [
        event
        async for event in engine.execute(
            session_id="session-1",
            system_prompt="Test",
            messages=[],
            max_iterations=2,
        )
    ]

    assert executor.called is False
    assert any(isinstance(event, AgentTextChunk) and "已重新规划" in event.content for event in events)
    assert len(captured_messages) == 2

    second_invoke_tool_messages = [message for message in captured_messages[1] if message.get("role") == "tool"]
    assert second_invoke_tool_messages
    feedback = second_invoke_tool_messages[-1]["content"]
    assert "FAILED" in feedback
    assert "semantic_validation_failed" in feedback
    assert "BASH_CONTAINER_REQUIRED" in feedback
