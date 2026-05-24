"""
InvestigationAgent 单元测试

测试覆盖：
  1. process()：路由模式判断（SOP / CDD / 降级）
  2. CDD 模式完成后应 yield AgentStageUpdate(stage="S4")
  3. kb_client 无知识时走降级路径
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from app.adapters.agents.htp.investigation_agent import InvestigationAgent
from app.domain.agent_port import AgentStageUpdate, AgentTextChunk


def _make_kb_client(
    route_result: dict | None = None,
    cases: list[dict] | None = None,
) -> MagicMock:
    """构建 KBClient mock"""
    mock = MagicMock()
    mock.route_by_category = AsyncMock(return_value=route_result)
    mock.search_cases_with_steps = AsyncMock(return_value=cases or [])
    return mock


def _make_registry_mock_with_stream(chunks: list[str]) -> MagicMock:
    """构建带流式输出的 AIRegistry mock"""
    mock_client = MagicMock()

    async def fake_stream(messages, system=None, **kwargs):
        for chunk in chunks:
            yield chunk

    mock_client.chat_completion_stream = fake_stream
    mock_registry = MagicMock()
    mock_registry.get_client.return_value = mock_client
    return mock_registry


def _make_kbd_diag_mock(stage_s4=True):
    """构建 KBDDiagnostic mock（注入到 InvestigationAgent 内部）"""
    from app.adapters.agents.htp.kbd_differential import KBDDiagResult

    mock = MagicMock()
    result = KBDDiagResult(
        matched_kbds=[],
        steps_executed=[],
        is_definitive=False,
        diagnosis_report="模拟 KBD 诊断报告",
    )
    mock.get_result.return_value = result

    async def fake_diagnose(candidates, env_context, session_id):
        if stage_s4:
            from app.domain.agent_port import AgentStageUpdate
            yield AgentStageUpdate(stage="kbd_diag_complete", metadata={})

    mock.diagnose = fake_diagnose
    return mock


class TestInvestigationAgentRouting:
    """process()：路由模式判断"""

    @pytest.mark.asyncio
    async def test_routes_to_sop_mode_when_sop_track(self):
        """route_by_category 返回 sop 轨道时走 SOP 模式"""
        kb = _make_kb_client(route_result={"track": "sop", "sop_document_id": 42, "sop_content": "SOP步骤内容"})
        registry = _make_registry_mock_with_stream(["SOP 诊断结论"])

        agent = InvestigationAgent(
            ai_registry=registry,
            kb_client=kb,
            tool_executor=MagicMock(),
        )

        events = [event async for event in agent.process(
            session_id="test-001",
            messages=[{"role": "user", "content": "虚拟机无法启动"}],
            category_id="虚拟机-003",
            diagnostic_stage="S1",
            env_context={},
            assistant_type="htp-agent",
            case_id=None,
            user_id="user-001",
        )]

        # 应有文本输出（来自 SOP 模式流式回复）
        text_events = [e for e in events if isinstance(e, AgentTextChunk)]
        assert len(text_events) >= 1
        assert "SOP 诊断结论" in text_events[-1].content

    @pytest.mark.asyncio
    async def test_routes_to_fallback_when_no_cases(self):
        """search_cases_with_steps 返回空列表时走降级模式"""
        kb = _make_kb_client(
            route_result={"track": "kbd"},
            cases=[],
        )
        registry = _make_registry_mock_with_stream(["机制推理结论"])

        agent = InvestigationAgent(
            ai_registry=registry,
            kb_client=kb,
            tool_executor=MagicMock(),
        )

        events = [event async for event in agent.process(
            session_id="test-002",
            messages=[{"role": "user", "content": "虚拟机无法启动"}],
            category_id="虚拟机-003",
            diagnostic_stage="S1",
            env_context={},
            assistant_type="htp-agent",
            case_id=None,
            user_id="user-001",
        )]

        text_events = [e for e in events if isinstance(e, AgentTextChunk)]
        assert len(text_events) >= 1

    @pytest.mark.asyncio
    async def test_kbd_diag_mode_yields_stage_s4(self):
        """KBD 差异诊断完成后应 yield AgentStageUpdate(stage='S4')"""
        kb = _make_kb_client(
            route_result={"track": "kbd"},
            cases=[
                {
                    "id": "c1",
                    "name": "案例c1",
                    "category_id": "虚拟机-003",
                    "steps": [
                        {"tool_name": "get_failed_tasks", "tool_args_template": {}, "expected_pattern": "__CONTAINS__:redis"}
                    ],
                    "root_cause": "redis 异常",
                    "solution": "重启 redis",
                    "similarity": 0.9,
                }
            ],
        )
        registry = _make_registry_mock_with_stream(["最终诊断报告内容"])

        # 工具执行器返回匹配输出
        mock_executor = MagicMock()
        async def execute(tool_name, args):
            return "redis service failed"
        mock_executor.execute = execute

        agent = InvestigationAgent(
            ai_registry=registry,
            kb_client=kb,
            tool_executor=mock_executor,
        )

        events = [event async for event in agent.process(
            session_id="test-003",
            messages=[{"role": "user", "content": "虚拟机无法启动"}],
            category_id="虚拟机-003",
            diagnostic_stage="S1",
            env_context={"vm_name": "vm-001"},
            assistant_type="htp-agent",
            case_id=None,
            user_id="user-001",
        )]

        stage_events = [e for e in events if isinstance(e, AgentStageUpdate)]
        final_stages = [e for e in stage_events if e.stage == "S4"]
        assert len(final_stages) == 1, f"未找到 S4 阶段事件，所有 stage 事件：{[e.stage for e in stage_events]}"
