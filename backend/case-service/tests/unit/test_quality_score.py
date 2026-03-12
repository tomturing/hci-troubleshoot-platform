"""
QualityScoreService 单元测试
"""

import pytest

# 激活 case-service 的 app 命名空间
from tests.conftest import *  # noqa: F401, F403

from app.services.quality_score import QualitySignals, compute_quality_score


class TestComputeQualityScore:
    """compute_quality_score 函数测试"""

    def test_user_command_fast_resolve_no_repeat(self):
        """测试用户主动关闭、快速解决、无重复提问的高分场景"""
        signals = QualitySignals(
            case_id="Q20260312001",
            close_reason="user_command",
            session_duration_sec=300,  # 5 分钟
            message_count=5,
            repeat_question_count=0,
        )

        result = compute_quality_score(signals)

        # 验收标准：composite_score ∈ [70, 100]
        assert 70 <= result.composite_score <= 100, f"预期分数在 70-100 之间，实际: {result.composite_score}"
        assert result.rating_included is False
        assert "close_intent" in result.breakdown
        assert result.breakdown["close_intent"] == 100  # user_command 最高分

    def test_abandon_slow_resolve_many_repeats(self):
        """测试用户放弃、慢速解决、多次重复提问的低分场景"""
        signals = QualitySignals(
            case_id="Q20260312002",
            close_reason="abandon",
            session_duration_sec=120,  # 2 分钟（短时间放弃，更差的信号）
            message_count=20,
            repeat_question_count=4,
        )

        result = compute_quality_score(signals)

        # 验收标准：composite_score ∈ [0, 40]
        assert 0 <= result.composite_score <= 40, f"预期分数在 0-40 之间，实际: {result.composite_score}"
        assert result.rating_included is False
        assert result.breakdown["close_intent"] == 10  # abandon 最低分
        assert result.breakdown["repeat_penalty"] == 0  # 4+ 次重复，0 分

    def test_timeout_long_session(self):
        """测试超时关闭但会话较长的情况（可能已解决）"""
        signals = QualitySignals(
            case_id="Q20260312003",
            close_reason="timeout",
            session_duration_sec=3700,  # > 1 小时
            message_count=10,
            repeat_question_count=1,
        )

        result = compute_quality_score(signals)

        # timeout > 1 小时，关闭意图分应为 70
        assert result.breakdown["close_intent"] == 70
        # 效率分：时长 > 1 小时 = 20，消息 10 轮 = 60，平均 = 40
        assert result.breakdown["efficiency"] == 40
        # 综合分应在中等偏上
        assert 50 <= result.composite_score <= 80

    def test_timeout_medium_session(self):
        """测试超时关闭且会话中等时长"""
        signals = QualitySignals(
            case_id="Q20260312004",
            close_reason="timeout",
            session_duration_sec=2000,  # 30-60 分钟
            message_count=8,
            repeat_question_count=0,
        )

        result = compute_quality_score(signals)

        # timeout 30-60 分钟，关闭意图分应为 55
        assert result.breakdown["close_intent"] == 55

    def test_timeout_short_session(self):
        """测试超时关闭且会话短（可能放弃）"""
        signals = QualitySignals(
            case_id="Q20260312005",
            close_reason="timeout",
            session_duration_sec=600,  # 10 分钟
            message_count=3,
            repeat_question_count=0,
        )

        result = compute_quality_score(signals)

        # timeout < 30 分钟，关闭意图分应为 40
        assert result.breakdown["close_intent"] == 40

    def test_with_user_rating(self):
        """测试有用户评分的情况"""
        signals = QualitySignals(
            case_id="Q20260312006",
            close_reason="user_command",
            session_duration_sec=600,
            message_count=4,
            repeat_question_count=0,
            user_rating=5,  # 5 星好评
        )

        result = compute_quality_score(signals)

        assert result.rating_included is True
        assert result.breakdown["user_rating"] == 100  # 5 星 = 100 分
        # 综合分应较高
        assert 80 <= result.composite_score <= 100

    def test_with_low_user_rating(self):
        """测试用户评分较低的情况"""
        signals = QualitySignals(
            case_id="Q20260312007",
            close_reason="user_command",
            session_duration_sec=1800,
            message_count=12,
            repeat_question_count=2,
            user_rating=2,  # 2 星差评
        )

        result = compute_quality_score(signals)

        assert result.rating_included is True
        assert result.breakdown["user_rating"] == 25  # 2 星 = 25 分
        # 综合分应较低
        assert 20 <= result.composite_score <= 60

    def test_with_ai_quality_data(self):
        """测试有 AI 质量数据的情况"""
        signals = QualitySignals(
            case_id="Q20260312008",
            close_reason="user_command",
            session_duration_sec=300,
            message_count=3,
            repeat_question_count=0,
            has_sop=True,
            kb_chunks_count=5,
            kb_top_score=0.85,
        )

        result = compute_quality_score(signals)

        assert "ai_quality" in result.breakdown
        # SOP 命中 +40，KB 相关度 0.85*60 = 51，总计 91
        assert result.breakdown["ai_quality"] == 91

    def test_no_ai_quality_data(self):
        """测试无 AI 质量数据的情况（权重转移）"""
        signals = QualitySignals(
            case_id="Q20260312009",
            close_reason="user_command",
            session_duration_sec=300,
            message_count=3,
            repeat_question_count=0,
            has_sop=None,
            kb_chunks_count=None,
        )

        result = compute_quality_score(signals)

        # 无 AI 质量数据时，该维度不参与计算
        assert "ai_quality" not in result.breakdown

    def test_admin_close(self):
        """测试管理员强制关闭"""
        signals = QualitySignals(
            case_id="Q20260312010",
            close_reason="admin_close",
            session_duration_sec=1200,
            message_count=6,
            repeat_question_count=0,
        )

        result = compute_quality_score(signals)

        assert result.breakdown["close_intent"] == 50

    def test_efficiency_score_calculation(self):
        """测试解决效率分计算"""
        # 快速解决：< 5 分钟，<= 3 轮
        signals_fast = QualitySignals(
            case_id="Q20260312011",
            close_reason="user_command",
            session_duration_sec=180,  # 3 分钟
            message_count=2,
            repeat_question_count=0,
        )
        result_fast = compute_quality_score(signals_fast)
        assert result_fast.breakdown["efficiency"] == 100  # (100 + 100) / 2 = 100

        # 慢速解决：> 60 分钟，> 25 轮
        signals_slow = QualitySignals(
            case_id="Q20260312012",
            close_reason="user_command",
            session_duration_sec=3700,  # > 1 小时
            message_count=30,  # > 25 轮
            repeat_question_count=0,
        )
        result_slow = compute_quality_score(signals_slow)
        assert result_slow.breakdown["efficiency"] == 20  # (20 + 20) / 2 = 20

    def test_repeat_question_penalty(self):
        """测试重复提问惩罚"""
        # 无重复
        signals_0 = QualitySignals(
            case_id="Q20260312013",
            close_reason="user_command",
            repeat_question_count=0,
        )
        assert compute_quality_score(signals_0).breakdown["repeat_penalty"] == 100

        # 1 次重复
        signals_1 = QualitySignals(
            case_id="Q20260312014",
            close_reason="user_command",
            repeat_question_count=1,
        )
        assert compute_quality_score(signals_1).breakdown["repeat_penalty"] == 70

        # 2 次重复
        signals_2 = QualitySignals(
            case_id="Q20260312015",
            close_reason="user_command",
            repeat_question_count=2,
        )
        assert compute_quality_score(signals_2).breakdown["repeat_penalty"] == 45

        # 3 次重复
        signals_3 = QualitySignals(
            case_id="Q20260312016",
            close_reason="user_command",
            repeat_question_count=3,
        )
        assert compute_quality_score(signals_3).breakdown["repeat_penalty"] == 20

        # 4+ 次重复
        signals_4 = QualitySignals(
            case_id="Q20260312017",
            close_reason="user_command",
            repeat_question_count=5,
        )
        assert compute_quality_score(signals_4).breakdown["repeat_penalty"] == 0

    def test_user_rating_mapping(self):
        """测试用户评分映射"""
        for rating, expected_score in [(1, 0), (2, 25), (3, 50), (4, 75), (5, 100)]:
            signals = QualitySignals(
                case_id=f"Q20260312{rating:02d}",
                close_reason="user_command",
                user_rating=rating,
            )
            result = compute_quality_score(signals)
            assert result.breakdown["user_rating"] == expected_score, f"评分 {rating} 星应映射为 {expected_score}"


class TestQualitySignals:
    """QualitySignals 数据结构测试"""

    def test_default_values(self):
        """测试默认值"""
        signals = QualitySignals(case_id="Q20260312001")

        assert signals.close_reason is None
        assert signals.session_duration_sec is None
        assert signals.message_count is None
        assert signals.repeat_question_count == 0
        assert signals.user_rating is None
        assert signals.has_sop is None
        assert signals.kb_chunks_count is None
        assert signals.kb_top_score is None

    def test_all_fields(self):
        """测试所有字段赋值"""
        signals = QualitySignals(
            case_id="Q20260312001",
            close_reason="user_command",
            session_duration_sec=300,
            message_count=5,
            repeat_question_count=2,
            user_rating=4,
            has_sop=True,
            kb_chunks_count=3,
            kb_top_score=0.75,
        )

        assert signals.case_id == "Q20260312001"
        assert signals.close_reason == "user_command"
        assert signals.session_duration_sec == 300
        assert signals.message_count == 5
        assert signals.repeat_question_count == 2
        assert signals.user_rating == 4
        assert signals.has_sop is True
        assert signals.kb_chunks_count == 3
        assert signals.kb_top_score == 0.75