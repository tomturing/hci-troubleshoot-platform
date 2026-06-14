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
from app.domain.agent_port import AgentInteractiveRequest

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
        assert result.needs_confirmation is True  # v3 改为全部需确认
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

    def test_confirmed_with_multi_level_prefix(self):
        """能识别多级前缀分类编码（如 虚拟机-L2-001）"""
        reply = "已确认故障分类：虚拟机-L2-001 虚拟机高级故障"
        result = self.triage._parse_intent_result(reply)

        assert result is not None
        assert result.category_id == "虚拟机-L2-001"
        assert result.category_name == "虚拟机高级故障"

    def test_candidates_with_multi_level_prefix(self):
        """候选列表中的编码也能匹配多级前缀"""
        reply = "可能是以下故障之一：\n① 硬件-L2-001 硬件高级故障\n② 存储-L3-002 存储三级故障\n"
        result = self.triage._parse_intent_result(reply)

        assert result is not None
        assert len(result.candidates) == 2
        assert result.candidates[0]["code"] == "硬件-L2-001"
        assert result.candidates[1]["code"] == "存储-L3-002"

    def test_leaf_code_regex_validation(self):
        """叶子节点 code 格式正则能匹配各种合法编码"""
        import re

        # 使用 TriageAgent 内部正则
        leaf_re = re.compile(r"^[一-鿿A-Za-z0-9-]+-\d+$")

        # 合法叶子节点编码（应匹配）
        assert leaf_re.match("虚拟机-003") is not None
        assert leaf_re.match("虚拟机-L2-001") is not None
        assert leaf_re.match("硬件-L3-002") is not None
        assert leaf_re.match("存储-001") is not None
        assert leaf_re.match("VM-005") is not None  # 英文前缀

        # 非叶子节点编码（应不匹配）
        assert leaf_re.match("虚拟机-L1") is None  # 无数字后缀
        assert leaf_re.match("虚拟机") is None  # 无后缀
        assert leaf_re.match("-003") is None  # 无前缀

    def test_custom_prompt_output(self):
        """能识别人工设置调优后输出的自定义高置信度格式（如 故障分类：硬件-024 硬盘寿命到期 95，高置信度）"""
        reply = (
            "监控到主机 SVR_aCloud_670 上的 SSD 磁盘寿命即将耗尽，触发紧急告警。\n\n"
            "故障分类：硬件-024 硬盘寿命到期 95，高置信度"
        )
        result = self.triage._parse_intent_result(reply)

        assert result is not None
        assert result.category_id == "硬件-024"
        assert result.category_name == "硬盘寿命到期"
        assert result.needs_confirmation is True
        assert len(result.candidates) == 0


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
    async def test_process_confirmed_intent_yields_interactive_request(self):
        """LLM 直接确认意图时，应 yield AgentInteractiveRequest（v3 改为全部需用户确认）"""
        mock_registry = MagicMock()
        mock_client = MagicMock()

        # 模拟分类缓存
        mock_kb = MagicMock()
        mock_kb.get_categories = AsyncMock(return_value=[{"code": "虚拟机-003", "name": "虚拟机开机失败"}])

        # 模拟 chat_completion_stream 返回确认文本
        async def fake_stream(messages, system=None, **kwargs):
            yield "已确认故障分类：虚拟机-003 虚拟机开机失败"

        mock_client.chat_completion_stream = fake_stream
        mock_registry.get_client.return_value = mock_client

        triage = TriageAgent(ai_registry=mock_registry, kb_client=mock_kb)
        # 直接注入分类缓存，跳过异步加载
        triage._categories_cache = {"虚拟机": [{"code": "虚拟机-003", "name": "虚拟机开机失败"}]}
        import time

        triage._categories_cache_time = time.time()

        events = [
            event
            async for event in triage.process(
                session_id="test-001",
                messages=[{"role": "user", "content": "我的虚拟机启动失败了"}],
                env_context={},
                assistant_type="htp-agent",
                case_id=None,
                user_id="user-001",
            )
        ]

        # v3 改为 yield AgentInteractiveRequest（单候选确认卡）
        interactive_events = [e for e in events if isinstance(e, AgentInteractiveRequest)]
        assert len(interactive_events) == 1
        assert interactive_events[0].kind == "intent_selection"
        assert interactive_events[0].metadata.get("category_id") == "虚拟机-003"
        assert interactive_events[0].metadata.get("single_candidate") is True

    @pytest.mark.asyncio
    async def test_process_candidates_yields_interactive_request(self):
        """LLM 返回候选列表时，应 yield AgentInteractiveRequest"""
        mock_registry = MagicMock()
        mock_client = MagicMock()

        mock_kb = MagicMock()
        mock_kb.get_categories = AsyncMock(
            return_value=[
                {"code": "虚拟机-001", "name": "虚拟机网络异常"},
                {"code": "虚拟机-003", "name": "虚拟机开机失败"},
            ]
        )

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

        events = [
            event
            async for event in triage.process(
                session_id="test-002",
                messages=[{"role": "user", "content": "网络或启动问题"}],
                env_context={},
                assistant_type="htp-agent",
                case_id=None,
                user_id="user-001",
            )
        ]

        interactive_events = [e for e in events if isinstance(e, AgentInteractiveRequest)]
        assert len(interactive_events) == 1
        assert interactive_events[0].kind == "intent_selection"
        assert len(interactive_events[0].metadata.get("candidates", [])) == 2
