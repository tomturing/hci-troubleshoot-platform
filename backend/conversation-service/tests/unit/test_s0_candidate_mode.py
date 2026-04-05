"""
S0 意图识别候选确认模式单元测试（v2）

覆盖范围：
  - parse_candidate_selection()：圆圈/阿拉伯数字/不匹配/边界
  - resolve_candidate_category()：序号映射/越界/选③
  - should_trigger_s0_failure()：轮次判断
  - _segment_s0_methodology()：关键约束词是否存在
  - _segment_s0_output_format()：两路分支格式是否正确
  - S0_MAX_CANDIDATE_ROUNDS 常量
"""

import pytest
from app.services.conversation_manager import (
    S0_MAX_CANDIDATE_ROUNDS,
    ConversationManager,
)
from app.services.prompt_builder import PromptBuilder

# ─── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def manager() -> ConversationManager:
    return ConversationManager()


@pytest.fixture
def builder() -> PromptBuilder:
    return PromptBuilder()


# 典型候选列表
CANDIDATES = [
    {"code": "虚拟机-003", "name": "虚拟机开机失败"},
    {"code": "存储-021", "name": "存储卷IO异常"},
]


# ─── parse_candidate_selection ────────────────────────────────────────────────


class TestParseCandidateSelection:
    """用户选择序号解析"""

    def test_circle_digit_1(self, manager: ConversationManager):
        assert manager.parse_candidate_selection("①") == 1

    def test_circle_digit_2(self, manager: ConversationManager):
        assert manager.parse_candidate_selection("②") == 2

    def test_circle_digit_3(self, manager: ConversationManager):
        assert manager.parse_candidate_selection("③") == 3

    def test_arabic_1(self, manager: ConversationManager):
        assert manager.parse_candidate_selection("1") == 1

    def test_arabic_2_with_dot(self, manager: ConversationManager):
        assert manager.parse_candidate_selection("2.") == 2

    def test_arabic_3_with_comma(self, manager: ConversationManager):
        assert manager.parse_candidate_selection("3、") == 3

    def test_circle_with_leading_space(self, manager: ConversationManager):
        assert manager.parse_candidate_selection("  ① ") == 1

    def test_arabic_with_text_after(self, manager: ConversationManager):
        """数字后跟文本（行首匹配）"""
        assert manager.parse_candidate_selection("1 然后继续") == 1

    def test_multiline_first_match(self, manager: ConversationManager):
        """多行取第一个匹配"""
        text = "好的\n②\n还有其他"
        assert manager.parse_candidate_selection(text) == 2

    def test_no_match_free_text(self, manager: ConversationManager):
        """自然语言补充描述不命中"""
        assert manager.parse_candidate_selection("我觉得是网络问题") is None

    def test_no_match_empty(self, manager: ConversationManager):
        assert manager.parse_candidate_selection("") is None

    def test_no_match_out_of_range(self, manager: ConversationManager):
        """数字 4、5 不命中（超出 ①②③ 范围）"""
        assert manager.parse_candidate_selection("4") is None

    def test_no_match_0(self, manager: ConversationManager):
        assert manager.parse_candidate_selection("0") is None

    def test_circle_in_middle_of_line_no_match(self, manager: ConversationManager):
        """圆圈数字在行中间不命中（只匹配行首）"""
        # 行首没有选择符
        result = manager.parse_candidate_selection("我选择①号方案")
        # 此处不在行首，取决于具体实现；当前实现是 splitlines 后逐行检测行首
        # 实际行首是"我选择①号方案"，正则从头匹配，应不命中
        assert result is None

    def test_fullwidth_space_leading(self, manager: ConversationManager):
        """全角空格前缀"""
        assert manager.parse_candidate_selection("\u3000②") == 2


# ─── resolve_candidate_category ──────────────────────────────────────────────


class TestResolveCandidateCategory:
    """候选序号到 category_info 的映射"""

    def test_select_first(self, manager: ConversationManager):
        result = manager.resolve_candidate_category(1, CANDIDATES)
        assert result == {"code": "虚拟机-003", "name": "虚拟机开机失败"}

    def test_select_second(self, manager: ConversationManager):
        result = manager.resolve_candidate_category(2, CANDIDATES)
        assert result == {"code": "存储-021", "name": "存储卷IO异常"}

    def test_select_third_none(self, manager: ConversationManager):
        """选 ③ 返回 None（以上都不是）"""
        result = manager.resolve_candidate_category(3, CANDIDATES)
        assert result is None

    def test_index_out_of_range(self, manager: ConversationManager):
        """候选列表只有 1 个，选 2 返回 None"""
        result = manager.resolve_candidate_category(2, CANDIDATES[:1])
        assert result is None

    def test_empty_candidates(self, manager: ConversationManager):
        result = manager.resolve_candidate_category(1, [])
        assert result is None

    def test_result_has_code_and_name(self, manager: ConversationManager):
        result = manager.resolve_candidate_category(1, CANDIDATES)
        assert result is not None
        assert "code" in result
        assert "name" in result

    def test_code_format(self, manager: ConversationManager):
        result = manager.resolve_candidate_category(1, CANDIDATES)
        assert result is not None
        # code 格式应为 域-序号
        assert "-" in result["code"]


# ─── should_trigger_s0_failure ───────────────────────────────────────────────


class TestShouldTriggerS0Failure:
    """S0 失败兜底触发条件"""

    def test_not_triggered_at_0(self):
        assert ConversationManager.should_trigger_s0_failure(0) is False

    def test_not_triggered_at_1(self):
        assert ConversationManager.should_trigger_s0_failure(1) is False

    def test_triggered_at_max(self):
        assert ConversationManager.should_trigger_s0_failure(S0_MAX_CANDIDATE_ROUNDS) is True

    def test_triggered_above_max(self):
        assert ConversationManager.should_trigger_s0_failure(S0_MAX_CANDIDATE_ROUNDS + 1) is True

    def test_constant_value(self):
        """S0_MAX_CANDIDATE_ROUNDS 默认应为 2"""
        assert S0_MAX_CANDIDATE_ROUNDS == 2

    def test_boundary_max_minus_1(self):
        """max-1 不应触发"""
        assert ConversationManager.should_trigger_s0_failure(S0_MAX_CANDIDATE_ROUNDS - 1) is False


# ─── PromptBuilder S0 方法论和输出格式 ───────────────────────────────────────


class TestS0PromptMethodology:
    """S0 Prompt 方法论（候选确认模式）内容校验"""

    def test_no_open_question_instruction(self, builder: PromptBuilder):
        """不应再有'提出1-3个精准确认问题'的指令"""
        methodology = builder._segment_s0_methodology()
        assert "提出 1-3 个精准确认问题" not in methodology
        assert "不要一次问超过 3 个" not in methodology

    def test_contains_classification_task_hint(self, builder: PromptBuilder):
        """应明确 S0 是分类任务而非对话任务"""
        methodology = builder._segment_s0_methodology()
        assert "分类任务" in methodology

    def test_contains_two_branch_hint(self, builder: PromptBuilder):
        """应包含两路分支（高/中置信度）"""
        methodology = builder._segment_s0_methodology()
        assert "高置信度" in methodology
        assert "中置信度" in methodology

    def test_contains_no_open_question_ban(self, builder: PromptBuilder):
        """应有明确禁止追问的说明"""
        methodology = builder._segment_s0_methodology()
        assert "禁止追问" in methodology

    def test_contains_no_guess_ban(self, builder: PromptBuilder):
        """应有明确禁止硬猜的说明"""
        methodology = builder._segment_s0_methodology()
        assert "禁止硬猜" in methodology


class TestS0PromptOutputFormat:
    """S0 输出格式规范（两路分支）"""

    def test_contains_direct_confirm_case(self, builder: PromptBuilder):
        """情况一：高置信度直接确认格式"""
        fmt = builder._segment_s0_output_format()
        assert "已确认故障分类" in fmt
        assert "80%" in fmt

    def test_contains_candidate_confirm_case(self, builder: PromptBuilder):
        """情况二：候选选项格式"""
        fmt = builder._segment_s0_output_format()
        assert "①" in fmt
        assert "②" in fmt
        assert "③" in fmt

    def test_contains_third_option_none(self, builder: PromptBuilder):
        """③ 必须是'以上都不是'"""
        fmt = builder._segment_s0_output_format()
        assert "以上都不是" in fmt

    def test_candidate_limit_2(self, builder: PromptBuilder):
        """候选最多 2 个"""
        fmt = builder._segment_s0_output_format()
        assert "最多 2 个" in fmt

    def test_ban_open_question(self, builder: PromptBuilder):
        """严格约束：禁止开放性问题"""
        fmt = builder._segment_s0_output_format()
        assert "开放性问题" in fmt

    def test_requires_evidence(self, builder: PromptBuilder):
        """每个候选需引用日志/告警依据"""
        fmt = builder._segment_s0_output_format()
        assert "判断依据" in fmt

    def test_example_category_format(self, builder: PromptBuilder):
        """示例格式包含完整 code + name"""
        fmt = builder._segment_s0_output_format()
        assert "虚拟机-003" in fmt


# ─── 候选选择 + 分类提取集成路径 ─────────────────────────────────────────────


class TestS0CandidateIntegration:
    """parse + resolve 组合测试（模拟 S0 候选确认一轮）"""

    def test_full_flow_select_1(self, manager: ConversationManager):
        user_reply = "① 是的，就是这个"
        selection = manager.parse_candidate_selection(user_reply)
        assert selection == 1
        result = manager.resolve_candidate_category(selection, CANDIDATES)
        assert result == {"code": "虚拟机-003", "name": "虚拟机开机失败"}

    def test_full_flow_select_2(self, manager: ConversationManager):
        user_reply = "2"
        selection = manager.parse_candidate_selection(user_reply)
        assert selection == 2
        result = manager.resolve_candidate_category(selection, CANDIDATES)
        assert result == {"code": "存储-021", "name": "存储卷IO异常"}

    def test_full_flow_select_none(self, manager: ConversationManager):
        """用户选 ③ → None → 触发轮次计数 → 超 2 轮触发兜底"""
        selection = manager.parse_candidate_selection("③")
        assert selection == 3
        result = manager.resolve_candidate_category(selection, CANDIDATES)
        assert result is None
        # 模拟第 2 轮
        assert ConversationManager.should_trigger_s0_failure(2) is True

    def test_no_match_no_failure_yet(self, manager: ConversationManager):
        """自然语言（未选择）不触发失败，由上层决定是否继续"""
        selection = manager.parse_candidate_selection("虚拟机的问题比较复杂")
        assert selection is None
        # 轮次为 1，还不触发兜底
        assert ConversationManager.should_trigger_s0_failure(1) is False

    def test_s0_new_methods_dont_affect_extract_category(
        self, manager: ConversationManager
    ):
        """新增方法不影响原有 extract_category 工作"""
        reply = "根据症状判断，已确认故障分类：虚拟机-003 虚拟机开机失败"
        result = manager.extract_category(reply)
        assert result is not None
        assert result["code"] == "虚拟机-003"
        assert result["name"] == "虚拟机开机失败"
