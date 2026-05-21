"""
Quality Score 单元测试
"""

from unittest.mock import AsyncMock

import pytest
from app.services.quality_score import (
    BASE_WEIGHTS,
    CLOSE_REASON_SCORE,
    QualityScore,
    QualityScoreService,
    QualitySignals,
    QualityStatsService,
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
        assert total == pytest.approx(1.0)

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


class TestQualityScoreService:
    """测试 QualityScoreService 类"""

    @pytest.fixture
    def mock_session(self):
        """Mock 数据库会话"""
        from unittest.mock import MagicMock

        session = MagicMock()
        session.execute = AsyncMock()
        session.commit = AsyncMock()
        return session

    @pytest.fixture
    def service(self, mock_session):
        """创建服务实例"""
        return QualityScoreService(mock_session)

    @pytest.mark.asyncio
    async def test_calculate_and_save_success(self, service, mock_session):
        """测试计算并保存质量评分"""
        from datetime import UTC, datetime, timedelta
        from unittest.mock import MagicMock

        # Mock conversation 查询结果
        conv_result = MagicMock()
        conv_result.fetchone.return_value = (
            5,  # message_count
            0,  # repeat_question_count
            datetime.now(UTC) - timedelta(minutes=5),  # started_at
            datetime.now(UTC),  # ended_at
        )

        # Mock prompt_audit 查询结果
        audit_result = MagicMock()
        audit_result.fetchone.return_value = (
            True,  # has_sop
            3,  # kb_chunks_count
            0.8,  # kb_top_score
        )

        # Mock evaluation 查询结果（无已存在）
        eval_result = MagicMock()
        eval_result.fetchone.return_value = None

        # Mock insert 执行结果
        insert_result = MagicMock()

        def mock_execute_side_effect(query, params=None):
            query_str_lower = str(query).lower()
            if "from conversation" in query_str_lower:
                return conv_result
            elif "from prompt_audit" in query_str_lower:
                return audit_result
            elif "select" in query_str_lower and "evaluation_id" in query_str_lower:
                return eval_result
            else:
                return insert_result

        mock_session.execute.side_effect = mock_execute_side_effect

        score = await service.calculate_and_save(
            case_id="TEST001",
            conversation_id="conv-001",
            close_reason="user_command",
            user_rating=5,
            trace_id="trace-001",
        )

        assert 0 <= score <= 100
        assert mock_session.commit.called

    @pytest.mark.asyncio
    async def test_calculate_and_save_no_conversation(self, service, mock_session):
        """测试无对话记录时的评分计算"""
        from unittest.mock import MagicMock

        # 使用调用计数器来区分不同的查询
        call_count = [0]

        def mock_execute_side_effect(query, params=None):
            call_count[0] += 1
            query_str = str(query)

            # 第 1 次调用：conversation 查询
            if "FROM conversation" in query_str:
                conv_result = MagicMock()
                conv_result.fetchone.return_value = None
                return conv_result

            # 第 2 次调用：assistant_evaluation 查询（_get_existing_rating）
            # 第 3 次调用：prompt_audit 查询
            # 第 4 次调用：assistant_evaluation 查询（_get_evaluation）
            # 第 5 次调用：INSERT 语句
            eval_result = MagicMock()
            eval_result.fetchone.return_value = None
            return eval_result

        mock_session.execute.side_effect = mock_execute_side_effect

        score = await service.calculate_and_save(
            case_id="TEST002",
            conversation_id=None,
            close_reason="abandon",
            user_rating=None,
        )

        # 无数据时应该返回有效分数
        assert 0 <= score <= 100

    @pytest.mark.asyncio
    async def test_update_user_rating(self, service, mock_session):
        """测试更新用户评分"""
        from datetime import UTC, datetime, timedelta
        from unittest.mock import MagicMock

        # Mock _get_evaluation 结果
        eval_result = MagicMock()
        eval_result.fetchone.return_value = (
            1,  # evaluation_id
            None,  # score
            "user_command",  # close_reason
            75,  # composite_score
        )

        # Mock conversation 查询结果
        conv_result = MagicMock()
        conv_result.fetchone.return_value = (
            5,  # message_count
            0,  # repeat_question_count
            datetime.now(UTC) - timedelta(minutes=5),  # started_at
            datetime.now(UTC),  # ended_at
        )

        # Mock prompt_audit 查询结果
        audit_result = MagicMock()
        audit_result.fetchone.return_value = (True, 3, 0.8)

        insert_result = MagicMock()

        def mock_execute_side_effect(query, params=None):
            query_str_lower = str(query).lower()
            if "select" in query_str_lower and "evaluation_id" in query_str_lower:
                return eval_result
            elif "from conversation" in query_str_lower:
                return conv_result
            elif "from prompt_audit" in query_str_lower:
                return audit_result
            else:
                return insert_result

        mock_session.execute.side_effect = mock_execute_side_effect

        score = await service.update_user_rating(
            case_id="TEST001",
            conversation_id="conv-001",
            user_rating=5,
        )

        assert 0 <= score <= 100


class TestQualityStatsService:
    """测试 QualityStatsService 类"""

    @pytest.fixture
    def mock_session(self):
        """Mock 数据库会话"""
        from unittest.mock import MagicMock

        session = MagicMock()
        session.execute = AsyncMock()
        return session

    @pytest.fixture
    def service(self, mock_session):
        """创建服务实例"""
        return QualityStatsService(mock_session)

    @pytest.mark.asyncio
    async def test_get_quality_stats(self, service, mock_session):
        """测试获取质量统计"""
        from unittest.mock import MagicMock

        # Mock 统计查询结果
        stats_result = MagicMock()
        stats_result.fetchone.return_value = (
            75.5,  # avg_score
            100,  # total_count
            15,  # low_score_count
        )

        # Mock 关闭原因分布结果
        reason_result = MagicMock()
        reason_result.fetchall.return_value = [
            ("user_command", 50),
            ("timeout", 30),
            ("abandon", 20),
        ]

        # Mock 评分分布结果
        rating_result = MagicMock()
        rating_result.fetchall.return_value = [
            ("5", 40),
            ("4", 30),
            ("unrated", 30),
        ]

        def mock_execute_side_effect(query, params=None):
            query_str = str(query)
            if "AVG(composite_score)" in query_str:
                return stats_result
            elif "close_reason" in query_str:
                return reason_result
            elif "score::text" in query_str:
                return rating_result
            return MagicMock()

        mock_session.execute.side_effect = mock_execute_side_effect

        stats = await service.get_quality_stats(days=7)

        assert stats["period_days"] == 7
        assert stats["average_composite_score"] == 75.5
        assert stats["total_evaluated_cases"] == 100
        assert stats["low_score_cases"] == 15
        assert "close_reason_distribution" in stats
        assert "user_rating_distribution" in stats

    @pytest.mark.asyncio
    async def test_get_low_score_cases(self, service, mock_session):
        """测试获取低分工单列表"""
        from datetime import UTC, datetime
        from unittest.mock import MagicMock

        # Mock 低分工单查询结果
        cases_result = MagicMock()
        cases_result.fetchall.return_value = [
            (
                "CASE001",
                "conv-001",
                2,  # user_rating
                25,  # composite_score
                "abandon",  # close_reason
                {"close_intent": 10},  # score_breakdown
                datetime.now(UTC),
                "测试工单",
                "closed",
            ),
        ]

        # Mock 总数查询结果
        count_result = MagicMock()
        count_result.scalar.return_value = 10

        def mock_execute_side_effect(query, params=None):
            query_str = str(query)
            if "COUNT(*)" in query_str:
                return count_result
            else:
                return cases_result

        mock_session.execute.side_effect = mock_execute_side_effect

        result = await service.get_low_score_cases(min_score=0, max_score=39, limit=20, offset=0)

        assert result["total"] == 10
        assert len(result["items"]) == 1
        assert result["items"][0]["case_id"] == "CASE001"
        assert result["items"][0]["composite_score"] == 25
