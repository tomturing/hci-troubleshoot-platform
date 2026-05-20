"""
Quality Score 单元测试
"""

import pytest
from app.services.quality_score import (
    BASE_WEIGHTS,
    CLOSE_REASON_SCORE,
    QualityScore,
    QualitySignals,
    _ai_quality_score,
    _duration_score,
    _message_count_score,
    _repeat_question_score,
    _timeout_score,
    compute_quality_score,
)


class TestHelperFunctions:
    """测试辅助函数"""

    def test_duration_score(self):
        """测试会话时长评分"""
        assert _duration_score(180) == 100  # < 5 min
        assert _duration_score(600) == 80  # 5-15 min
        assert _duration_score(1200) == 60  # 15-30 min
        assert _duration_score(2400) == 40  # 30-60 min
        assert _duration_score(3600) == 20  # > 60 min
        assert _duration_score(None) == 50  # 无数据

    def test_message_count_score(self):
        """测试消息轮数评分"""
        assert _message_count_score(2) == 100  # <= 3 轮
        assert _message_count_score(5) == 80  # 4-8 轮
        assert _message_count_score(10) == 60  # 9-15 轮
        assert _message_count_score(20) == 40  # 16-25 轮
        assert _message_count_score(30) == 20  # > 25 轮
        assert _message_count_score(None) == 50  # 无数据

    def test_repeat_question_score(self):
        """测试重复提问评分"""
        assert _repeat_question_score(0) == 100
        assert _repeat_question_score(1) == 70
        assert _repeat_question_score(2) == 45
        assert _repeat_question_score(3) == 20
        assert _repeat_question_score(4) == 0
        assert _repeat_question_score(10) == 0

    def test_ai_quality_score(self):
        """测试 AI 能力质量评分"""
        # 有 SOP 且 KB 命中质量高
        assert _ai_quality_score(True, 3, 0.9) == 94  # 40 + min(60, 54) = 94
        # 有 SOP 但无 KB
        assert _ai_quality_score(True, 0, 0.0) == 40
        # 无 SOP 但有 KB
        assert _ai_quality_score(False, 3, 0.8) == 48  # 0 + min(60, 48) = 48
        # 无 SOP 无 KB
        assert _ai_quality_score(False, 0, 0.0) == 0
        # KB 相关度很低
        assert _ai_quality_score(True, 5, 0.1) == 46  # 40 + 6 = 46

    def test_timeout_score(self):
        """测试超时评分修正"""
        assert _timeout_score(4000) == 70  # > 1 小时
        assert _timeout_score(2700) == 55  # 30-60 分钟
        assert _timeout_score(1801) == 55  # > 30 分钟
        assert _timeout_score(1800) == 40  # = 30 分钟（边界值）
        assert _timeout_score(1200) == 40  # < 30 分钟


class TestComputeQualityScore:
    """测试综合评分计算"""

    def test_full_dimensions_with_rating(self):
        """测试完整维度（含用户评分）"""
        signals = QualitySignals(
            case_id="TEST001",
            close_reason="user_command",
            session_duration_sec=300,  # 5 min → 80
            message_count=5,  # → 80
            repeat_question_count=0,  # → 100
            user_rating=5,  # → 100
            has_sop=True,
            kb_chunks_count=3,
            kb_top_score=0.8,
        )

        result = compute_quality_score(signals)

        assert isinstance(result, QualityScore)
        assert result.rating_included is True
        assert 0 <= result.composite_score <= 100
        assert "user_rating" in result.breakdown
        assert "close_intent" in result.breakdown
        assert "efficiency" in result.breakdown
        assert "repeat_penalty" in result.breakdown
        assert "ai_quality" in result.breakdown

    def test_without_user_rating(self):
        """测试无用户评分时的降级模型"""
        signals = QualitySignals(
            case_id="TEST002",
            close_reason="user_command",
            session_duration_sec=300,
            message_count=5,
            repeat_question_count=0,
            user_rating=None,  # 无用户评分
            has_sop=True,
            kb_chunks_count=3,
            kb_top_score=0.8,
        )

        result = compute_quality_score(signals)

        assert result.rating_included is False
        assert "user_rating" not in result.breakdown
        # 其他维度应该仍然存在
        assert "close_intent" in result.breakdown
        assert "efficiency" in result.breakdown

    def test_abandon_case_low_score(self):
        """测试放弃 case 的低分情况"""
        signals = QualitySignals(
            case_id="TEST003",
            close_reason="abandon",  # 放弃 → 低分
            session_duration_sec=600,
            message_count=30,  # 消息很多
            repeat_question_count=3,  # 重复提问多
            user_rating=None,
            has_sop=False,
            kb_chunks_count=0,
            kb_top_score=0.0,
        )

        result = compute_quality_score(signals)

        # 放弃 + 消息多 + 重复提问 = 应该分数较低
        assert result.composite_score < 50
        assert result.breakdown["close_intent"] == 10  # abandon 是 10 分
        assert result.breakdown["repeat_penalty"] == 20  # 重复 3 次是 20 分

    def test_user_command_high_score(self):
        """测试用户主动关闭的高分情况"""
        signals = QualitySignals(
            case_id="TEST004",
            close_reason="user_command",
            session_duration_sec=180,  # 3 min，快速解决
            message_count=2,  # 很少的消息
            repeat_question_count=0,  # 无重复
            user_rating=5,  # 5 星好评
            has_sop=True,
            kb_chunks_count=5,
            kb_top_score=0.9,
        )

        result = compute_quality_score(signals)

        # 主动关闭 + 快速解决 + 5 星 = 高分
        assert result.composite_score >= 80
        assert result.breakdown["user_rating"] == 100
        assert result.breakdown["close_intent"] == 100

    def test_minimal_data(self):
        """测试最小数据情况"""
        signals = QualitySignals(
            case_id="TEST005",
            close_reason=None,
            session_duration_sec=None,
            message_count=None,
            repeat_question_count=0,
            user_rating=None,
            has_sop=None,
            kb_chunks_count=None,
            kb_top_score=None,
        )

        result = compute_quality_score(signals)

        # 应该能正常计算，使用默认值
        assert 0 <= result.composite_score <= 100

    def test_extreme_values(self):
        """测试极端值"""
        signals = QualitySignals(
            case_id="TEST006",
            close_reason="abandon",
            session_duration_sec=86400,  # 24 小时
            message_count=100,
            repeat_question_count=10,
            user_rating=1,  # 1 星
            has_sop=False,
            kb_chunks_count=0,
            kb_top_score=0.0,
        )

        result = compute_quality_score(signals)

        # 应该返回较低分数
        assert result.composite_score < 30
        assert result.breakdown["user_rating"] == 0  # 1 星 → 0 分

    def test_timeout_with_duration(self):
        """测试超时场景结合时长"""
        signals = QualitySignals(
            case_id="TEST007",
            close_reason="timeout",
            session_duration_sec=4000,  # > 1 小时 → 应该修正为 70 分
            message_count=10,
            repeat_question_count=0,
            user_rating=None,
            has_sop=True,
            kb_chunks_count=2,
            kb_top_score=0.7,
        )

        result = compute_quality_score(signals)
        assert result.breakdown["close_intent"] == 70

    def test_without_ai_quality_data(self):
        """测试无 AI 质量数据的情况"""
        signals = QualitySignals(
            case_id="TEST008",
            close_reason="user_command",
            session_duration_sec=300,
            message_count=5,
            repeat_question_count=0,
            user_rating=4,
            has_sop=None,  # 无 AI 质量数据
            kb_chunks_count=None,
            kb_top_score=None,
        )

        result = compute_quality_score(signals)
        assert "ai_quality" not in result.breakdown


class TestCloseReasonMapping:
    """测试关闭原因映射"""

    def test_close_reason_scores(self):
        """验证关闭原因评分映射"""
        assert CLOSE_REASON_SCORE["user_command"] == 100
        assert CLOSE_REASON_SCORE["timeout"] == 50
        assert CLOSE_REASON_SCORE["admin_close"] == 50
        assert CLOSE_REASON_SCORE["abandon"] == 10
        assert CLOSE_REASON_SCORE[None] == 50


class TestBaseWeights:
    """测试基础权重配置"""

    def test_weights_sum_to_one(self):
        """验证权重之和为 1.0"""
        total = sum(BASE_WEIGHTS.values())
        assert total == 1.0

    def test_weights_structure(self):
        """验证权重结构"""
        assert "user_rating" in BASE_WEIGHTS
        assert "close_intent" in BASE_WEIGHTS
        assert "efficiency" in BASE_WEIGHTS
        assert "repeat_penalty" in BASE_WEIGHTS
        assert "ai_quality" in BASE_WEIGHTS


class TestQualitySignals:
    """测试 QualitySignals 数据类"""

    def test_create_signals(self):
        """测试创建信号对象"""
        signals = QualitySignals(
            case_id="TEST001",
            close_reason="user_command",
            session_duration_sec=300,
            message_count=5,
            repeat_question_count=0,
            user_rating=5,
            has_sop=True,
            kb_chunks_count=3,
            kb_top_score=0.8,
        )
        assert signals.case_id == "TEST001"
        assert signals.close_reason == "user_command"
        assert signals.user_rating == 5


class TestQualityScore:
    """测试 QualityScore 数据类"""

    def test_create_score(self):
        """测试创建评分对象"""
        score = QualityScore(
            composite_score=85,
            rating_included=True,
            breakdown={"user_rating": 100, "close_intent": 100, "efficiency": 80},
        )
        assert score.composite_score == 85
        assert score.rating_included is True
        assert len(score.breakdown) == 3
