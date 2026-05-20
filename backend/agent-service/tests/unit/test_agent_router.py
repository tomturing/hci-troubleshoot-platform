"""
AgentRouter 单元测试
"""

import pytest

from app.adapters.agent_router import AgentRouter, OPS_AGENT_TYPE, PYDANTIC_AI_TYPE
from app.core.agent_port import AgentTextChunk, AgentUnavailableError


class MockHTPAdapter:
    """Mock HTPAgentAdapter"""

    def __init__(self, should_fail=False):
        self.should_fail = should_fail
        self.call_count = 0

    async def process(self, *, session_id, messages, **kwargs):
        self.call_count += 1
        if self.should_fail:
            raise AgentUnavailableError("htp", "mock failure")
        yield AgentTextChunk(content="HTP 响应")
        yield AgentTextChunk(content="完成")


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

    async def process(self, *, session_id, messages, **kwargs):
        self.call_count += 1
        if self.should_fail:
            raise AgentUnavailableError("pydantic-ai", "服务不可用")
        yield AgentTextChunk(content="Pydantic-AI 响应")


class MockAIRegistry:
    """Mock AIAssistantRegistry"""

    def get_default_type(self):
        return "glm-4-flash"


@pytest.fixture
def htp_adapter():
    """HTP adapter fixture"""
    return MockHTPAdapter()


@pytest.fixture
def ops_adapter():
    """Ops agent adapter fixture"""
    return MockOpsAdapter()


@pytest.fixture
def pai_adapter():
    """Pai adapter fixture"""
    return MockPaiAdapter()


@pytest.fixture
def ai_registry():
    """AI registry fixture"""
    return MockAIRegistry()


class TestAgentRouterRouting:
    """AgentRouter 路由测试"""

    @pytest.mark.asyncio
    async def test_route_to_htp_by_default(self, htp_adapter, ai_registry):
        """测试默认路由到 HTP"""
        router = AgentRouter(htp_adapter=htp_adapter, ai_registry=ai_registry)

        events = [event async for event in router.process(
            assistant_type="glm-4-flash",
            session_id="test-001",
            messages=[{"role": "user", "content": "test"}]
        )]

        assert len(events) == 2
        assert htp_adapter.call_count == 1
        assert events[0].content == "HTP 响应"

    @pytest.mark.asyncio
    async def test_route_to_ops_agent_when_enabled(self, htp_adapter, ops_adapter, ai_registry):
        """测试路由到 ops-agent（当启用时）"""
        router = AgentRouter(
            htp_adapter=htp_adapter,
            ops_agent_adapter=ops_adapter,
            ai_registry=ai_registry
        )

        events = [event async for event in router.process(
            assistant_type=OPS_AGENT_TYPE,
            session_id="test-002",
            messages=[{"role": "user", "content": "test"}]
        )]

        assert len(events) == 1
        assert ops_adapter.call_count == 1
        assert htp_adapter.call_count == 0
        assert events[0].content == "Ops-Agent 响应"

    @pytest.mark.asyncio
    async def test_route_to_pai_when_enabled(self, htp_adapter, pai_adapter, ai_registry):
        """测试路由到 pydantic-ai（当启用时）"""
        router = AgentRouter(
            htp_adapter=htp_adapter,
            pai_adapter=pai_adapter,
            ai_registry=ai_registry
        )

        events = [event async for event in router.process(
            assistant_type=PYDANTIC_AI_TYPE,
            session_id="test-003",
            messages=[{"role": "user", "content": "test"}]
        )]

        assert len(events) == 1
        assert pai_adapter.call_count == 1
        assert htp_adapter.call_count == 0
        assert events[0].content == "Pydantic-AI 响应"


class TestAgentRouterFallback:
    """AgentRouter 降级测试"""

    @pytest.mark.asyncio
    async def test_ops_agent_disabled_fallback_to_htp(self, htp_adapter, ai_registry):
        """测试 ops-agent 未启用时降级到 HTP"""
        router = AgentRouter(htp_adapter=htp_adapter, ai_registry=ai_registry)

        events = [event async for event in router.process(
            assistant_type=OPS_AGENT_TYPE,
            session_id="test-004",
            messages=[{"role": "user", "content": "test"}]
        )]

        # 应该降级到 HTP，第一条消息是降级通知
        assert len(events) >= 2
        assert htp_adapter.call_count == 1
        assert "系统提示" in events[0].content

    @pytest.mark.asyncio
    async def test_ops_agent_failure_fallback_to_htp(self, htp_adapter, ai_registry):
        """测试 ops-agent 失败时降级到 HTP"""
        failing_ops = MockOpsAdapter(should_fail=True)
        router = AgentRouter(
            htp_adapter=htp_adapter,
            ops_agent_adapter=failing_ops,
            ai_registry=ai_registry
        )

        events = [event async for event in router.process(
            assistant_type=OPS_AGENT_TYPE,
            session_id="test-005",
            messages=[{"role": "user", "content": "test"}]
        )]

        assert failing_ops.call_count == 1
        assert htp_adapter.call_count == 1
        assert "系统提示" in events[0].content

    @pytest.mark.asyncio
    async def test_pai_disabled_fallback_to_htp(self, htp_adapter, ai_registry):
        """测试 pai 未启用时降级到 HTP"""
        router = AgentRouter(htp_adapter=htp_adapter, ai_registry=ai_registry)

        events = [event async for event in router.process(
            assistant_type=PYDANTIC_AI_TYPE,
            session_id="test-006",
            messages=[{"role": "user", "content": "test"}]
        )]

        assert htp_adapter.call_count == 1

    @pytest.mark.asyncio
    async def test_pai_failure_fallback_to_htp(self, htp_adapter, ai_registry):
        """测试 pai 失败时降级到 HTP"""
        failing_pai = MockPaiAdapter(should_fail=True)
        router = AgentRouter(
            htp_adapter=htp_adapter,
            pai_adapter=failing_pai,
            ai_registry=ai_registry
        )

        events = [event async for event in router.process(
            assistant_type=PYDANTIC_AI_TYPE,
            session_id="test-007",
            messages=[{"role": "user", "content": "test"}]
        )]

        assert failing_pai.call_count == 1
        assert htp_adapter.call_count == 1


class TestAgentRouterGetOpsAdapter:
    """测试获取 ops adapter"""

    def test_get_ops_adapter_when_present(self, htp_adapter, ops_adapter, ai_registry):
        """测试获取 ops adapter"""
        router = AgentRouter(
            htp_adapter=htp_adapter,
            ops_agent_adapter=ops_adapter,
            ai_registry=ai_registry
        )
        assert router.get_ops_agent_adapter() is ops_adapter

    def test_get_ops_adapter_when_not_present(self, htp_adapter, ai_registry):
        """测试 ops adapter 不存在时返回 None"""
        router = AgentRouter(htp_adapter=htp_adapter, ai_registry=ai_registry)
        assert router.get_ops_agent_adapter() is None
