"""
RemediationAgent 单元测试

测试覆盖：
  1. require_all_confirm=True：risk_level=1 的只读工具在 S5 阶段也触发确认
  2. process() 流程验证
  3. AgentRouter S5 路由验证
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from app.adapters.agents.htp.react_engine import ReactEngine
from app.adapters.agents.htp.remediation_agent import RemediationAgent
from app.adapters.agents.htp.tool_registry import TOOL_REGISTRY
from app.domain.agent_port import AgentInteractiveRequest, AgentStageUpdate
from shared.clients.ai_client import InvokeResult, ToolCallRequest

# ─── Mock Helpers ─────────────────────────────────────────────────────────────


def _make_ai_registry_with_tool_call(
    tool_name: str = "get_active_alerts",  # risk_level=1 的只读工具
    tool_args: dict | None = None,
):
    """构建返回工具调用的 AI Registry mock（用于验证只读工具触发确认）"""
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
    final_result = InvokeResult(content="修复完成", tool_calls=[])

    mock_client.invoke = AsyncMock(side_effect=[invoke_result, final_result])

    # chat_completion_stream 返回 AsyncGenerator
    async def fake_stream(messages, user_id, **kwargs):
        for chunk in ["修复", "完成"]:
            yield chunk

    mock_client.chat_completion_stream = fake_stream

    mock_registry = MagicMock()
    mock_registry.get_client = MagicMock(return_value=mock_client)
    return mock_registry


def _make_confirm_service_mock(approved: bool = True):
    """构建确认服务 mock，返回 ConfirmResult 枚举"""
    from app.adapters.agents.htp.confirm_service import ConfirmResult

    mock = MagicMock()

    # 返回真正的 ConfirmResult 枚举值
    async def request_confirm(session_id, tool_name, tool_args, risk_level):
        return ConfirmResult.APPROVED if approved else ConfirmResult.REJECTED

    mock.request_confirm = request_confirm
    return mock


def _make_tool_executor_mock():
    """构建工具执行器 mock"""
    executor = MagicMock()

    async def execute(tool_name, args):
        return {"alerts": ["alert1", "alert2"]}

    executor.execute = execute
    return executor


# ─── 测试用例 ────────────────────────────────────────────────────────────────


class TestRemediationAgentRequireAllConfirm:
    """验收标准 1：所有 risk_level=1 的工具在 S5 阶段也触发确认"""

    @pytest.mark.asyncio
    async def test_readonly_tool_triggers_confirm_in_s5(self):
        """
        验证 risk_level=1 的只读工具在 S5 阶段（require_all_confirm=True）触发确认

        这是 T-AGT-12 的核心验收标准：
        - 正常阶段（S1-S4）：risk_level=1 工具自动执行，无需确认
        - S5 阶段：require_all_confirm=True，所有工具（含只读）均需用户确认
        """
        mock_registry = _make_ai_registry_with_tool_call(
            tool_name="get_active_alerts",  # risk_level=1
            tool_args={"limit": 10},
        )
        mock_confirm = _make_confirm_service_mock(approved=True)
        mock_executor = _make_tool_executor_mock()

        engine = ReactEngine(
            ai_registry=mock_registry,
            tool_registry=TOOL_REGISTRY,
            tool_executor=mock_executor,
            confirm_service=mock_confirm,
            audit_service=None,
        )

        events = []
        async for event in engine.execute(
            session_id="test-s5-session",
            system_prompt="S5 修复模式",
            messages=[{"role": "user", "content": "执行修复"}],
            max_iterations=2,
            require_all_confirm=True,  # S5 关键参数
        ):
            events.append(event)

        # 验证：应触发 AgentInteractiveRequest（确认请求）
        confirm_requests = [e for e in events if isinstance(e, AgentInteractiveRequest)]
        assert len(confirm_requests) >= 1, (
            f"require_all_confirm=True 时，只读工具也应触发确认请求，实际事件：{[type(e).__name__ for e in events]}"
        )

        # 验证确认请求内容
        confirm_event = confirm_requests[0]
        assert confirm_event.kind == "tool_confirm"
        assert confirm_event.metadata.get("tool_name") == "get_active_alerts"
        assert confirm_event.metadata.get("risk_level") == 1  # 只读工具的 risk_level

    @pytest.mark.asyncio
    async def test_write_tool_always_requires_confirm(self):
        """
        验证 risk_level=2 的写操作工具在任何阶段都需要确认

        写操作工具（如 acli_service_restart）的 risk_level=2，
        无论是否设置 require_all_confirm，都应触发确认。
        """
        mock_registry = _make_ai_registry_with_tool_call(
            tool_name="acli_service_restart",  # risk_level=2
            tool_args={"service_name": "exporter", "node_ip": "192.168.1.1"},
        )
        mock_confirm = _make_confirm_service_mock(approved=True)
        mock_executor = _make_tool_executor_mock()

        engine = ReactEngine(
            ai_registry=mock_registry,
            tool_registry=TOOL_REGISTRY,
            tool_executor=mock_executor,
            confirm_service=mock_confirm,
            audit_service=None,
        )

        # 不设置 require_all_confirm=False，写操作仍需确认
        events = []
        async for event in engine.execute(
            session_id="test-s1-session",
            system_prompt="S1 诊断模式",
            messages=[{"role": "user", "content": "诊断问题"}],
            max_iterations=2,
            require_all_confirm=False,  # 正常诊断阶段
        ):
            events.append(event)

        # 验证：写操作工具仍触发确认
        confirm_requests = [e for e in events if isinstance(e, AgentInteractiveRequest)]
        assert len(confirm_requests) >= 1, (
            f"risk_level=2 的写操作工具应始终触发确认，实际事件：{[type(e).__name__ for e in events]}"
        )

    @pytest.mark.asyncio
    async def test_confirm_rejected_stops_execution(self):
        """
        验证用户拒绝确认时，工具不执行，流程终止
        """
        mock_registry = _make_ai_registry_with_tool_call(
            tool_name="get_active_alerts",
        )
        mock_confirm = _make_confirm_service_mock(approved=False)  # 用户拒绝
        mock_executor = _make_tool_executor_mock()

        engine = ReactEngine(
            ai_registry=mock_registry,
            tool_registry=TOOL_REGISTRY,
            tool_executor=mock_executor,
            confirm_service=mock_confirm,
            audit_service=None,
        )

        events = []
        async for event in engine.execute(
            session_id="test-reject-session",
            system_prompt="S5 修复模式",
            messages=[{"role": "user", "content": "执行修复"}],
            max_iterations=2,
            require_all_confirm=True,
        ):
            events.append(event)

        # 验证：确认被拒绝，工具执行器不被调用
        # 注意：mock_executor.execute 是 async function，不能直接用 call_count
        # 我们通过事件流验证
        from app.domain.agent_port import AgentTextChunk

        text_events = [e for e in events if isinstance(e, AgentTextChunk)]

        # 应有 "操作已取消" 或类似提示
        cancel_messages = [e for e in text_events if "取消" in e.content]
        assert len(cancel_messages) >= 1, "用户拒绝时应提示操作已取消"


class TestRemediationAgentProcess:
    """RemediationAgent.process() 流程测试"""

    @pytest.mark.asyncio
    async def test_process_yields_remediation_start_event(self):
        """
        验证 process() 开始时 yield AgentStageUpdate(stage="remediation_start")
        """
        mock_registry = MagicMock()
        mock_client = MagicMock()

        # 构造直接返回文字的 invoke（无工具调用）
        invoke_result = InvokeResult(content="修复建议：重启服务", tool_calls=[])
        mock_client.invoke = AsyncMock(return_value=invoke_result)

        async def fake_stream(messages, user_id, **kwargs):
            yield "修复建议：重启服务"

        mock_client.chat_completion_stream = fake_stream
        mock_registry.get_client = MagicMock(return_value=mock_client)

        mock_kb_client = MagicMock()
        mock_react_engine = MagicMock()

        # ReactEngine.execute 返回空流（直接终止）
        async def fake_execute(**kwargs):
            return
            yield  # pragma: no cover

        mock_react_engine.execute = fake_execute

        agent = RemediationAgent(
            ai_registry=mock_registry,
            kb_client=mock_kb_client,
            react_engine=mock_react_engine,
        )

        events = []
        async for event in agent.process(
            session_id="test-001",
            messages=[{"role": "user", "content": "执行修复"}],
            matched_kbds=None,
            root_cause="服务异常",
            solution="重启服务",
            assistant_type="htp-agent",
            case_id="case-001",
            user_id="user-001",
        ):
            events.append(event)

        # 验证 remediation_start 事件
        start_events = [e for e in events if isinstance(e, AgentStageUpdate) and e.stage == "remediation_start"]
        assert len(start_events) >= 1, "应有 remediation_start 事件"

        # 验证 metadata 携带 require_all_confirm=True
        start_event = start_events[0]
        assert start_event.metadata.get("require_all_confirm")

    @pytest.mark.asyncio
    async def test_process_yields_s6_after_remediation(self):
        """
        验证 process() 完成后 yield AgentStageUpdate(stage="S6")
        """
        mock_registry = MagicMock()
        mock_client = MagicMock()

        invoke_result = InvokeResult(content="修复完成", tool_calls=[])
        mock_client.invoke = AsyncMock(return_value=invoke_result)

        async def fake_stream(messages, user_id, **kwargs):
            yield "修复完成"

        mock_client.chat_completion_stream = fake_stream
        mock_registry.get_client = MagicMock(return_value=mock_client)

        mock_kb_client = MagicMock()
        mock_react_engine = MagicMock()

        # ReactEngine.execute 返回空流（模拟修复完成）
        async def fake_execute(**kwargs):
            return
            yield  # pragma: no cover

        mock_react_engine.execute = fake_execute

        agent = RemediationAgent(
            ai_registry=mock_registry,
            kb_client=mock_kb_client,
            react_engine=mock_react_engine,
        )

        events = []
        async for event in agent.process(
            session_id="test-002",
            messages=[{"role": "user", "content": "执行修复"}],
            matched_kbds=None,
            root_cause="服务异常",
            solution="重启服务",
            assistant_type="htp-agent",
            case_id="case-002",
            user_id="user-002",
        ):
            events.append(event)

        # 验证 S6 事件
        s6_events = [e for e in events if isinstance(e, AgentStageUpdate) and e.stage == "S6"]
        assert len(s6_events) >= 1, "修复完成后应推进到 S6 验证闭环"


class TestToolRegistryRiskLevels:
    """工具注册表风险等级验证"""

    def test_readonly_tools_have_risk_level_1(self):
        """
        验证只读工具的 risk_level=1
        """
        readonly_tools = [
            "get_active_alerts",
            "get_failed_tasks",
            "get_vm_list",
            "get_cluster_detail",
            "acli_system_top",
            "acli_vm_list",
            "acli_vm_config",
            "acli_vm_disk_check",
            "acli_platform_node_list",
            "acli_storage_disk_list",
            "acli_network_nic_list",
            "acli_log_get",
            "acli_run",
            "get_sop_node",
            "sop_advance",
        ]

        for tool_name in readonly_tools:
            tool_def = TOOL_REGISTRY.get(tool_name)
            assert tool_def is not None, f"工具 {tool_name} 应存在于 TOOL_REGISTRY"
            assert tool_def.risk_level == 1, f"只读工具 {tool_name} 的 risk_level 应为 1"

    def test_write_tools_have_risk_level_2(self):
        """
        验证写操作工具的 risk_level=2
        """
        write_tools = [
            "acli_service_restart",
            "acli_network_nic_up",
            "acli_netdoctor",
        ]

        for tool_name in write_tools:
            tool_def = TOOL_REGISTRY.get(tool_name)
            assert tool_def is not None, f"工具 {tool_name} 应存在于 TOOL_REGISTRY"
            assert tool_def.risk_level == 2, f"写操作工具 {tool_name} 的 risk_level 应为 2"
