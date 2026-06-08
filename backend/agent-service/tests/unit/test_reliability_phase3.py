from unittest.mock import AsyncMock, MagicMock

import pytest
from app.adapters.agents.htp.react_engine import ReactEngine
from app.domain.agent_port import AgentTextChunk, ToolResultEvent
from app.services.hallucination_detector import HallucinationDetector
from pydantic import BaseModel, Field

# ─── 1. HallucinationDetector Tests ─────────────────────────────────────────

def test_hallucination_detector_phantom_tool():
    detector = HallucinationDetector(tool_registry={
        "acli_vm_list": MagicMock(),
        "acli_service_restart": MagicMock()
    })

    # Mentioned VM list, but never executed
    text = "根据 acli_vm_list 的输出，我们发现虚拟机正常。"
    report = detector.detect(
        llm_text=text,
        executed_tools=["acli_service_restart"],
        tool_outputs=["Service restarted successfully"]
    )
    assert report["has_hallucination"] is True
    assert "acli_vm_list" in report["phantom_tools"]


def test_hallucination_detector_overconfident():
    detector = HallucinationDetector()

    # Overconfident claim with no uncertainty words
    text = "已确认是虚拟机网络配置文件损坏导致的故障。"
    report = detector.detect(
        llm_text=text,
        executed_tools=[],
        tool_outputs=[]
    )
    assert report["has_hallucination"] is True
    assert len(report["overconfident_claims"]) > 0

    # Clean claim with uncertainty word
    text2 = "已确认是虚拟机网络配置文件损坏，可能是由于底层异常导致的。"
    report2 = detector.detect(
        llm_text=text2,
        executed_tools=[],
        tool_outputs=[]
    )
    assert report2["has_hallucination"] is False


def test_hallucination_detector_ungrounded_number():
    detector = HallucinationDetector()

    # Contains a percentage/decimal not in tool outputs
    text = "当前磁盘空间错误率为 98.5%，需要立即扩容。"
    report = detector.detect(
        llm_text=text,
        executed_tools=[],
        tool_outputs=["Disk health is normal, usage is 50%"]
    )
    assert report["has_hallucination"] is True
    assert "98.5%" in report["ungrounded_numbers"]

    # Grounded percentage
    text2 = "当前磁盘使用率为 50%"
    report2 = detector.detect(
        llm_text=text2,
        executed_tools=[],
        tool_outputs=["Disk health is normal, usage is 50%"]
    )
    assert report2["has_hallucination"] is False


# ─── 2. ReactEngine Schema Validation & Blocking Tests ──────────────────────

class SimpleSchema(BaseModel):
    name: str = Field(description="VM name")
    status: str = Field(description="VM status")


@pytest.mark.asyncio
async def test_react_engine_schema_validation_success():
    ai_client = MagicMock()
    ai_client.invoke = AsyncMock(return_value=MagicMock(
        content='{"name": "vm-1", "status": "running"}',
        tool_calls=[]
    ))

    async def mock_stream(*args, **kwargs):
        yield '{"name": "vm-1", "status": "running"}'
    ai_client.chat_completion_stream = mock_stream

    ai_registry = MagicMock()
    ai_registry.get_client = MagicMock(return_value=ai_client)

    engine = ReactEngine(
        ai_registry=ai_registry,
        tool_registry={},
        tool_executor=MagicMock()
    )

    events = []
    async for event in engine.execute(
        session_id="session-1",
        system_prompt="Test",
        messages=[],
        response_schema=SimpleSchema
    ):
        events.append(event)

    assert engine.schema_validation_failed is False
    # Verified that text is streamed
    text_chunks = [e.content for e in events if isinstance(e, AgentTextChunk)]
    assert len(text_chunks) > 0
    assert '{"name": "vm-1"' in "".join(text_chunks)


@pytest.mark.asyncio
async def test_react_engine_schema_validation_failure_blocks_write():
    ai_client = MagicMock()
    # Bad JSON content
    ai_client.invoke = AsyncMock(return_value=MagicMock(
        content='{"name": "vm-1", invalid_json}',
        tool_calls=[]
    ))

    async def mock_stream(*args, **kwargs):
        yield '{"name": "vm-1", invalid_json}'
    ai_client.chat_completion_stream = mock_stream

    ai_registry = MagicMock()
    ai_registry.get_client = MagicMock(return_value=ai_client)

    engine = ReactEngine(
        ai_registry=ai_registry,
        tool_registry={
            "acli_service_restart": MagicMock(risk_level=2)
        },
        tool_executor=MagicMock()
    )

    events = []
    async for event in engine.execute(
        session_id="session-1",
        system_prompt="Test",
        messages=[],
        response_schema=SimpleSchema
    ):
        events.append(event)

    # Schema validation failed
    assert engine.schema_validation_failed is True

    # Now verify that executing a risk=2 tool is blocked in this engine instance
    tool_call_dict = {"name": "acli_service_restart", "args": {"service": "vm"}}
    tool_events = []
    async for event in engine._execute_tool_call(
        tool_call=tool_call_dict,
        session_id="session-1",
        step=1
    ):
        tool_events.append(event)

    # Execution is blocked and returns error in result
    tool_result_events = [e for e in tool_events if isinstance(e, ToolResultEvent)]
    assert len(tool_result_events) == 1
    assert "前序推理格式校验失败" in tool_result_events[0].result["error"]


# ─── 3. Verification priority closed-loop test ──────────────────────────────

@pytest.mark.asyncio
async def test_react_engine_verification_priority_blocks_closure():
    ai_client = MagicMock()
    # First invoke claims closure ("问题已解决"), but a write operation was executed and no verification occurred
    ai_client.invoke = AsyncMock()
    ai_client.invoke.side_effect = [
        MagicMock(content="问题已解决，虚拟机服务已恢复正常运行。", tool_calls=[]),
        MagicMock(content="验证结束，已正常。", tool_calls=[])  # Second invoke
    ]

    async def mock_stream(*args, **kwargs):
        yield "验证结束，已正常。"
    ai_client.chat_completion_stream = mock_stream

    ai_registry = MagicMock()
    ai_registry.get_client = MagicMock(return_value=ai_client)

    engine = ReactEngine(
        ai_registry=ai_registry,
        tool_registry={},
        tool_executor=MagicMock()
    )

    # Simulate that we executed a write operation (e.g. restart) but haven't run any verification
    engine.has_write_operation = True
    engine.has_verification_after_write = False

    events = []
    async for event in engine.execute(
        session_id="session-1",
        system_prompt="Test",
        messages=[],
        max_iterations=2  # Allow up to 2 steps so it can iterate after interception
    ):
        events.append(event)

    # Intercepted and appended system instruction, triggering next loop execution
    assert ai_client.invoke.call_count == 2
