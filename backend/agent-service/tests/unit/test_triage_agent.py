"""
TriageAgent 单元测试

测试覆盖：
  1. _parse_intent_result：意图解析逻辑（确认 / 候选 / 未知）
  2. resolve_candidate_selection：候选选择处理
  3. process()：流程测试（mock LLM，验证 yield 事件）
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from app.adapters.agents.htp.triage_agent import TriageAgent
from app.domain.agent_port import AgentInteractiveRequest, AgentStageUpdate

# ─── _parse_intent_result 测试 ────────────────────────────────────────────────


class TestParseIntentResult:
    """_parse_intent_result：意图解析静态方法"""

    def setup_method(self):
        self.triage = TriageAgent(ai_registry=MagicMock(), kb_client=MagicMock())

    def test_confirmed_pattern(self):
        """能识别'已确认故障分类'格式"""
        reply = "已确认故障分类：虚拟机-003 虚拟机开机失败"
        result = self.triage._parse_intent_result(reply)

        assert result is not None
        assert result.needs_confirmation is False
        assert result.category_id == "虚拟机-003"
        assert "虚拟机开机失败" in result.category_name

    def test_candidates_pattern(self):
        """能识别①②候选格式"""
        reply = (
            "根据您的描述，可能是以下故障之一：\n"
            "① 虚拟机-001 虚拟机网络异常\n"
            "② 虚拟机-003 虚拟机开机失败\n"
            "请问您的虚拟机是否完全无法启动？"
        )
        result = self.triage._parse_intent_result(reply)

        assert result is not None
        assert result.needs_confirmation is True
        assert len(result.candidates) == 2
        assert result.candidates[0]["code"] == "虚拟机-001"
        assert result.candidates[1]["code"] == "虚拟机-003"

    def test_unknown_intent(self):
        """无法识别意图时返回 category_id=None, candidates 为空（LLM 通用回复场景）"""
        reply = "您好，请问您遇到了什么问题？"
        result = self.triage._parse_intent_result(reply)

        assert result is not None
        assert result.category_id is None
        assert len(result.candidates) == 0

    def test_confirmed_with_different_format(self):
        """允许 ID 与名称之间有多个空格"""
        reply = "已确认故障分类：存储-007  存储卷IO异常"
        result = self.triage._parse_intent_result(reply)

        assert result is not None
        assert result.category_id == "存储-007"


# ─── resolve_candidate_selection 测试 ───────────────────────────────────────


class TestResolveCandidateSelection:
    """resolve_candidate_selection：候选选择处理"""

    def setup_method(self):
        self.triage = TriageAgent(ai_registry=MagicMock(), kb_client=MagicMock())
        self.candidates = [
            {"code": "虚拟机-001", "name": "虚拟机网络异常"},
            {"code": "虚拟机-003", "name": "虚拟机开机失败"},
        ]

    @pytest.mark.asyncio
    async def test_select_first_option(self):
        """选择第一个候选"""
        result = await self.triage.resolve_candidate_selection(1, self.candidates)
        assert result is not None
        assert result.category_id == "虚拟机-001"

    @pytest.mark.asyncio
    async def test_select_second_option(self):
        """选择第二个候选"""
        result = await self.triage.resolve_candidate_selection(2, self.candidates)
        assert result is not None
        assert result.category_id == "虚拟机-003"

    @pytest.mark.asyncio
    async def test_select_none_option(self):
        """选择 3（'以上都不是'）时返回 category_id=None"""
        result = await self.triage.resolve_candidate_selection(3, self.candidates)
        assert result is not None
        assert result.category_id is None

    @pytest.mark.asyncio
    async def test_select_out_of_range(self):
        """选择超出候选数量时返回 category_id=None"""
        result = await self.triage.resolve_candidate_selection(5, self.candidates)
        assert result is not None
        assert result.category_id is None


# ─── process() 流程测试 ──────────────────────────────────────────────────────


def _make_stream_mock(text: str):
    """构建模拟流式 LLM 响应的 AsyncGenerator"""
    async def _gen():
        yield text

    return _gen()


class TestTriageAgentProcess:
    """process()：mock LLM 后的流程验证"""

    @pytest.mark.asyncio
    async def test_process_confirmed_intent_yields_stage_s1(self):
        """LLM 直接确认意图时，应 yield AgentStageUpdate(stage='S1')"""
        mock_registry = MagicMock()
        mock_client = MagicMock()

        # 模拟分类缓存
        mock_kb = MagicMock()
        mock_kb.get_categories = AsyncMock(return_value=[
            {"code": "虚拟机-003", "name": "虚拟机开机失败"}
        ])

        # 模拟 chat_completion_stream 返回确认文本
        async def fake_stream(messages, system=None, **kwargs):
            yield "已确认故障分类：虚拟机-003 虚拟机开机失败"

        mock_client.chat_completion_stream = fake_stream
        mock_registry.get_client.return_value = mock_client

        triage = TriageAgent(ai_registry=mock_registry, kb_client=mock_kb)
        # 直接注入分类缓存，跳过异步加载
        triage._categories_cache = {
            "虚拟机": [{"code": "虚拟机-003", "name": "虚拟机开机失败"}]
        }
        import time
        triage._categories_cache_time = time.time()

        events = [event async for event in triage.process(
            session_id="test-001",
            messages=[{"role": "user", "content": "我的虚拟机启动失败了"}],
            env_context={},
            assistant_type="htp-agent",
            case_id=None,
            user_id="user-001",
        )]

        # 最后一个事件应是 AgentStageUpdate(stage="S1")
        stage_events = [e for e in events if isinstance(e, AgentStageUpdate)]
        assert len(stage_events) >= 1
        assert stage_events[-1].stage == "S1"
        assert stage_events[-1].metadata.get("category_id") == "虚拟机-003"

    @pytest.mark.asyncio
    async def test_process_candidates_yields_interactive_request(self):
        """LLM 返回候选列表时，应 yield AgentInteractiveRequest"""
        mock_registry = MagicMock()
        mock_client = MagicMock()

        mock_kb = MagicMock()
        mock_kb.get_categories = AsyncMock(return_value=[
            {"code": "虚拟机-001", "name": "虚拟机网络异常"},
            {"code": "虚拟机-003", "name": "虚拟机开机失败"},
        ])

        async def fake_stream(messages, system=None, **kwargs):
            yield "可能是以下故障之一：\n① 虚拟机-001 虚拟机网络异常\n② 虚拟机-003 虚拟机开机失败\n请确认"

        mock_client.chat_completion_stream = fake_stream
        mock_registry.get_client.return_value = mock_client

        triage = TriageAgent(ai_registry=mock_registry, kb_client=mock_kb)
        # 直接注入分类缓存
        triage._categories_cache = {
            "虚拟机": [
                {"code": "虚拟机-001", "name": "虚拟机网络异常"},
                {"code": "虚拟机-003", "name": "虚拟机开机失败"},
            ]
        }
        import time
        triage._categories_cache_time = time.time()

        events = [event async for event in triage.process(
            session_id="test-002",
            messages=[{"role": "user", "content": "网络或启动问题"}],
            env_context={},
            assistant_type="htp-agent",
            case_id=None,
            user_id="user-001",
        )]

        interactive_events = [e for e in events if isinstance(e, AgentInteractiveRequest)]
        assert len(interactive_events) == 1
        assert interactive_events[0].kind == "intent_selection"
        assert len(interactive_events[0].metadata.get("candidates", [])) == 2
