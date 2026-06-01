"""
AgentRouter 单元测试（v4.3 架构）

测试路由逻辑：
  - ops-agent → OpsAgentAdapter（含降级）
  - pai-agent → PaiAgentAdapter（含降级）
  - htp-agent + S0          → TriageAgent（T-AGT-10：S0 意图识别）
  - htp-agent + S1/S2/S3/S4 → InvestigationAgent（T-AGT-11：S1-S4 诊断调查）
  - htp-agent + S5          → RemediationAgent（T-AGT-12：S5 修复执行）
"""

import pytest
from app.adapters.agents.agent_router import OPS_AGENT_TYPE, PYDANTIC_AI_TYPE, AgentRouter
from app.domain.agent_port import AgentStageUpdate, AgentTextChunk, AgentUnavailableError


class MockTriageAgent:  # T-AGT-10：Mock 类名也改为 Triage
    """Mock TriageAgent（S0 意图识别）"""

    def __init__(self, should_fail=False):
        self.should_fail = should_fail
        self.call_count = 0

    async def process(self, *, session_id, messages, **kwargs):
        self.call_count += 1
        if self.should_fail:
            raise AgentUnavailableError("triage-agent", "mock failure")
        yield AgentTextChunk(content="已识别意图：虚拟机启动失败")
        yield AgentStageUpdate(stage="S1", metadata={"category_id": "虚拟机-003"})


class MockInvestigationAgent:  # T-AGT-11：新增 Mock
    """Mock InvestigationAgent（S1-S4 诊断调查）"""

    def __init__(self, should_fail=False):
        self.should_fail = should_fail
        self.call_count = 0
        self.last_category_id = None
        self.last_stage = None

    async def process(self, *, session_id, messages, category_id, diagnostic_stage, **kwargs):
        self.call_count += 1
        self.last_category_id = category_id
        self.last_stage = diagnostic_stage
        if self.should_fail:
            raise AgentUnavailableError("investigation-agent", "mock failure")
        yield AgentTextChunk(content=f"诊断进行中（{diagnostic_stage}）：{category_id}")
        yield AgentStageUpdate(stage="S4", metadata={"matched_cases": ["case-001"]})


class MockRemediationAgent:
    """Mock RemediationAgent（S5 修复执行）"""

    def __init__(self):
        self.call_count = 0

    async def process(self, *, session_id, messages, **kwargs):
        self.call_count += 1
        yield AgentTextChunk(content="修复方案：重启虚拟机")
        yield AgentStageUpdate(stage="S6", metadata={"note": "修复完成"})


class MockOpsAdapter:
    """Mock OpsAgentAdapter"""

    def __init__(self, should_fail=False):
        self.should_fail = should_fail
        self.call_count = 0

    async def process(self, *, session_id, messages, **kwargs):
        self.call_count += 1
        if self.should_fail:
            raise AgentUnavailableError("ops-agent", "连接超时")
        yield AgentTextChunk(content="Ops-Agent 响应")


class MockPaiAdapter:
    """Mock PaiAgentAdapter"""

    def __init__(self, should_fail=False):
        self.should_fail = should_fail
        self.call_count = 0
        self.last_category_id = None

    async def process(self, *, session_id, messages, category_id=None, **kwargs):
        self.call_count += 1
        self.last_category_id = category_id
        if self.should_fail:
            raise AgentUnavailableError("pydantic-ai", "服务不可用")
        if category_id:
            yield AgentTextChunk(content=f"Pydantic-AI 响应（分类：{category_id}）")
        else:
            yield AgentTextChunk(content="Pydantic-AI 响应")


class MockAIRegistry:
    """Mock AIAssistantRegistry"""

    def get_default_type(self):
        return "htp-agent"


@pytest.fixture
def triage_agent():  # T-AGT-10：fixture 名改为 triage_agent
    return MockTriageAgent()


@pytest.fixture
def investigation_agent():  # T-AGT-11：新增 fixture
    return MockInvestigationAgent()


@pytest.fixture
def remediation_agent():
    return MockRemediationAgent()


@pytest.fixture
def ops_adapter():
    return MockOpsAdapter()


@pytest.fixture
def pai_adapter():
    return MockPaiAdapter()


@pytest.fixture
def ai_registry():
    return MockAIRegistry()


class TestAgentRouterRouting:
    """AgentRouter 核心路由测试（v4.3）"""

    @pytest.mark.asyncio
    async def test_route_s0_to_triage_agent(
        self, triage_agent, investigation_agent, ai_registry
    ):
        """S0 阶段应路由到 TriageAgent（T-AGT-10）"""
        router = AgentRouter(
            triage_agent=triage_agent,
            investigation_agent=investigation_agent,
            ai_registry=ai_registry,
        )

        events = [event async for event in router.process(
            assistant_type="htp-agent",
            session_id="test-001",
            messages=[{"role": "user", "content": "虚拟机启动失败"}],
            diagnostic_stage="S0",
        )]

        assert triage_agent.call_count == 1
        assert investigation_agent.call_count == 0
        assert len(events) >= 1
        assert "已识别意图" in events[0].content

    @pytest.mark.asyncio
    async def test_route_s1_to_investigation_agent(
        self, triage_agent, investigation_agent, ai_registry
    ):
        """S1 阶段应路由到 InvestigationAgent（T-AGT-11）"""
        router = AgentRouter(
            triage_agent=triage_agent,
            investigation_agent=investigation_agent,
            ai_registry=ai_registry,
        )

        events = [event async for event in router.process(
            assistant_type="htp-agent",
            session_id="test-002",
            messages=[{"role": "user", "content": "继续诊断"}],
            diagnostic_stage="S1",
            category_id="虚拟机-003",
        )]

        assert triage_agent.call_count == 0
        assert investigation_agent.call_count == 1
        assert investigation_agent.last_category_id == "虚拟机-003"
        assert investigation_agent.last_stage == "S1"
        assert "诊断进行中" in events[0].content

    @pytest.mark.asyncio
    async def test_route_all_diagnostic_stages(
        self, triage_agent, investigation_agent, ai_registry
    ):
        """S1/S2/S3/S4 均应路由到 InvestigationAgent（T-AGT-11）"""
        router = AgentRouter(
            triage_agent=triage_agent,
            investigation_agent=investigation_agent,
            ai_registry=ai_registry,
        )

        for stage in ("S1", "S2", "S3", "S4"):
            investigation_agent.call_count = 0
            events = [event async for event in router.process(
                assistant_type="htp-agent",
                session_id=f"test-stage-{stage}",
                messages=[{"role": "user", "content": "test"}],
                diagnostic_stage=stage,
                category_id="虚拟机-003",
            )]
            assert investigation_agent.call_count == 1, f"{stage} 未路由到 InvestigationAgent"

    @pytest.mark.asyncio
    async def test_route_s5_to_remediation_agent(
        self, triage_agent, investigation_agent, remediation_agent, ai_registry
    ):
        """S5 阶段应路由到 RemediationAgent"""
        router = AgentRouter(
            triage_agent=triage_agent,
            investigation_agent=investigation_agent,
            remediation_agent=remediation_agent,
            ai_registry=ai_registry,
        )

        events = [event async for event in router.process(
            assistant_type="htp-agent",
            session_id="test-s5",
            messages=[{"role": "user", "content": "执行修复"}],
            diagnostic_stage="S5",
            category_id="虚拟机-003",
        )]

        assert remediation_agent.call_count == 1
        assert triage_agent.call_count == 0
        assert investigation_agent.call_count == 0
        # 最终事件应推进到 S6
        stage_events = [e for e in events if isinstance(e, AgentStageUpdate)]
        assert any(e.stage == "S6" for e in stage_events)

    @pytest.mark.asyncio
    async def test_s5_missing_remediation_agent_returns_notice(
        self, triage_agent, investigation_agent, ai_registry
    ):
        """S5 阶段 RemediationAgent 未注入时返回提示"""
        router = AgentRouter(
            triage_agent=triage_agent,
            investigation_agent=investigation_agent,
            ai_registry=ai_registry,
            # 未注入 remediation_agent
        )

        events = [event async for event in router.process(
            assistant_type="htp-agent",
            session_id="test-s5-no-agent",
            messages=[{"role": "user", "content": "执行修复"}],
            diagnostic_stage="S5",
            category_id="虚拟机-003",
        )]

        assert len(events) == 1
        assert "暂不可用" in events[0].content or "提示" in events[0].content

    @pytest.mark.asyncio
    async def test_s1_missing_category_id_returns_error(
        self, triage_agent, investigation_agent, ai_registry
    ):
        """S1+ 阶段缺少 category_id 时返回错误"""
        router = AgentRouter(
            triage_agent=triage_agent,
            investigation_agent=investigation_agent,

            ai_registry=ai_registry,
        )

        events = [event async for event in router.process(
            assistant_type="htp-agent",
            session_id="test-003",
            messages=[{"role": "user", "content": "继续诊断"}],
            diagnostic_stage="S1",
            category_id=None,
        )]

        assert triage_agent.call_count == 0
        assert investigation_agent.call_count == 0

        assert len(events) == 1
        assert "错误" in events[0].content

    @pytest.mark.asyncio
    async def test_route_to_ops_agent_when_enabled(
        self, triage_agent, investigation_agent, ops_adapter, ai_registry
    ):
        """ops-agent 启用时路由到 OpsAgentAdapter"""
        router = AgentRouter(
            triage_agent=triage_agent,
            investigation_agent=investigation_agent,

            ops_agent_adapter=ops_adapter,
            ai_registry=ai_registry,
        )

        events = [event async for event in router.process(
            assistant_type=OPS_AGENT_TYPE,
            session_id="test-004",
            messages=[{"role": "user", "content": "test"}],
        )]

        assert ops_adapter.call_count == 1
        assert triage_agent.call_count == 0
        assert investigation_agent.call_count == 0

        assert events[0].content == "Ops-Agent 响应"

    @pytest.mark.asyncio
    async def test_route_to_pai_when_enabled(
        self, triage_agent, investigation_agent, pai_adapter, ai_registry
    ):
        """pai-agent 启用时路由到 PaiAgentAdapter"""
        router = AgentRouter(
            triage_agent=triage_agent,
            investigation_agent=investigation_agent,

            pai_adapter=pai_adapter,
            ai_registry=ai_registry,
        )

        events = [event async for event in router.process(
            assistant_type=PYDANTIC_AI_TYPE,
            session_id="test-005",
            messages=[{"role": "user", "content": "test"}],
            category_id="虚拟机-003",  # 测试 category_id 传递
        )]

        assert pai_adapter.call_count == 1
        assert triage_agent.call_count == 0
        assert pai_adapter.last_category_id == "虚拟机-003"
        assert "分类：虚拟机-003" in events[0].content


class TestAgentRouterFallback:
    """AgentRouter 降级测试（v4.2）"""

    @pytest.mark.asyncio
    async def test_ops_agent_disabled_fallback_to_investigation(
        self, triage_agent, investigation_agent, ai_registry
    ):
        """ops-agent 未启用时应降级到 InvestigationAgent（T-AGT-11）"""
        router = AgentRouter(
            triage_agent=triage_agent,
            investigation_agent=investigation_agent,

            ai_registry=ai_registry,
        )

        events = [event async for event in router.process(
            assistant_type=OPS_AGENT_TYPE,
            session_id="test-006",
            messages=[{"role": "user", "content": "test"}],
            diagnostic_stage="S1",
            category_id="虚拟机-003",
        )]

        # 应降级到 InvestigationAgent，第一条是降级通知
        assert investigation_agent.call_count == 1

        assert any("系统提示" in e.content for e in events if isinstance(e, AgentTextChunk))

    @pytest.mark.asyncio
    async def test_ops_agent_failure_fallback_to_investigation(
        self, triage_agent, investigation_agent, ai_registry
    ):
        """ops-agent 调用失败时应降级到 InvestigationAgent（T-AGT-11）"""
        failing_ops = MockOpsAdapter(should_fail=True)
        router = AgentRouter(
            triage_agent=triage_agent,
            investigation_agent=investigation_agent,

            ops_agent_adapter=failing_ops,
            ai_registry=ai_registry,
        )

        events = [event async for event in router.process(
            assistant_type=OPS_AGENT_TYPE,
            session_id="test-007",
            messages=[{"role": "user", "content": "test"}],
            diagnostic_stage="S1",
            category_id="虚拟机-003",
        )]

        assert failing_ops.call_count == 1
        assert investigation_agent.call_count == 1


    @pytest.mark.asyncio
    async def test_pai_disabled_fallback_to_investigation(
        self, triage_agent, investigation_agent, ai_registry
    ):
        """pai-agent 未启用时降级到 InvestigationAgent（T-AGT-11）"""
        router = AgentRouter(
            triage_agent=triage_agent,
            investigation_agent=investigation_agent,

            ai_registry=ai_registry,
        )

        events = [event async for event in router.process(
            assistant_type=PYDANTIC_AI_TYPE,
            session_id="test-008",
            messages=[{"role": "user", "content": "test"}],
            diagnostic_stage="S1",
            category_id="虚拟机-003",
        )]

        assert investigation_agent.call_count == 1


    @pytest.mark.asyncio
    async def test_pai_failure_fallback_to_investigation(
        self, triage_agent, investigation_agent, ai_registry
    ):
        """pai-agent 调用失败时降级到 InvestigationAgent（T-AGT-11）"""
        failing_pai = MockPaiAdapter(should_fail=True)
        router = AgentRouter(
            triage_agent=triage_agent,
            investigation_agent=investigation_agent,

            pai_adapter=failing_pai,
            ai_registry=ai_registry,
        )

        events = [event async for event in router.process(
            assistant_type=PYDANTIC_AI_TYPE,
            session_id="test-009",
            messages=[{"role": "user", "content": "test"}],
            diagnostic_stage="S1",
            category_id="虚拟机-003",
        )]

        assert failing_pai.call_count == 1
        assert investigation_agent.call_count == 1



class TestAgentRouterGetOpsAdapter:
    """测试 get_ops_agent_adapter()"""

    def test_get_ops_adapter_when_present(
        self, triage_agent, investigation_agent, ops_adapter, ai_registry
    ):
        """OpsAdapter 注入时应正确返回"""
        router = AgentRouter(
            triage_agent=triage_agent,
            investigation_agent=investigation_agent,

            ops_agent_adapter=ops_adapter,
            ai_registry=ai_registry,
        )
        assert router.get_ops_agent_adapter() is ops_adapter

    def test_get_ops_adapter_when_not_present(
        self, triage_agent, investigation_agent, ai_registry
    ):
        """未注入 OpsAdapter 时应返回 None"""
        router = AgentRouter(
            triage_agent=triage_agent,
            investigation_agent=investigation_agent,

            ai_registry=ai_registry,
        )
        assert router.get_ops_agent_adapter() is None
