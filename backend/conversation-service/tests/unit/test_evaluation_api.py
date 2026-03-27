"""
测试 Evaluation API - 用户评分接口
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.routes.evaluate import (
    EvaluationCreate,
    get_low_score_cases,
    get_quality_stats,
    require_admin_token,
    submit_evaluation,
)
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.fixture
def mock_session():
    """创建模拟数据库会话"""
    return AsyncMock(spec=AsyncSession)


class TestEvaluationAPI:
    """测试评分 API 接口"""

    @pytest.mark.asyncio
    async def test_submit_evaluation_success(self, mock_session):
        """测试提交评分成功"""
        conversation_id = uuid.uuid4()

        mock_conv = MagicMock()
        mock_conv.case_id = "TEST001"
        mock_conv.trace_id = "trace-1"

        conv_result = MagicMock()
        conv_result.scalar_one_or_none.return_value = mock_conv
        existing_result = MagicMock()
        existing_result.fetchone.return_value = None
        mock_session.execute.side_effect = [conv_result, existing_result]

        with patch(
            "app.routes.evaluate.QualityScoreService.update_user_rating",
            new=AsyncMock(return_value=82),
        ):
            result = await submit_evaluation(
                conversation_id=conversation_id,
                evaluation=EvaluationCreate(score=4),
                session=mock_session,
            )

        assert result.ok is True
        assert result.score == 4
        assert result.composite_score == 82

    def test_submit_evaluation_invalid_score(self):
        """测试提交无效评分"""
        with pytest.raises(ValidationError):
            EvaluationCreate(score=6)

    @pytest.mark.asyncio
    async def test_submit_evaluation_conversation_not_found(self, mock_session):
        """测试对话不存在"""
        conversation_id = uuid.uuid4()

        conv_result = MagicMock()
        conv_result.scalar_one_or_none.return_value = None
        mock_session.execute.side_effect = [conv_result]

        with pytest.raises(HTTPException) as exc_info:
            await submit_evaluation(
                conversation_id=conversation_id,
                evaluation=EvaluationCreate(score=4),
                session=mock_session,
            )

        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_get_quality_stats_admin(self, mock_session):
        """测试获取质量统计（Admin）"""
        with patch(
            "app.services.quality_score.QualityStatsService.get_quality_stats",
            new=AsyncMock(
                return_value={
                    "period_days": 7,
                    "average_composite_score": 75.5,
                    "total_evaluated_cases": 100,
                    "low_score_cases": 5,
                    "low_score_rate": 5.0,
                    "close_reason_distribution": {"user_command": 80, "timeout": 15, "abandon": 5},
                    "user_rating_distribution": {"4": 30, "5": 40, "unrated": 30},
                }
            ),
        ):
            result = await get_quality_stats(days=7, _=None, session=mock_session)

        assert result.average_composite_score == 75.5
        assert "user_command" in result.close_reason_distribution

    @pytest.mark.asyncio
    async def test_get_low_score_cases_admin(self, mock_session):
        """测试获取低分工件列表（Admin）"""
        with patch(
            "app.services.quality_score.QualityStatsService.get_low_score_cases",
            new=AsyncMock(
                return_value={
                    "items": [
                        {
                            "case_id": "TEST001",
                            "conversation_id": str(uuid.uuid4()),
                            "user_rating": None,
                            "composite_score": 35,
                            "close_reason": "abandon",
                            "score_breakdown": None,
                            "evaluated_at": None,
                            "case_title": "测试工单",
                            "case_status": "closed",
                        }
                    ],
                    "total": 1,
                    "limit": 20,
                    "offset": 0,
                }
            ),
        ):
            result = await get_low_score_cases(
                min_score=0,
                max_score=39,
                limit=20,
                offset=0,
                _=None,
                session=mock_session,
            )

        assert result.total == 1
        assert result.items[0].composite_score == 35

    def test_admin_api_requires_token(self):
        """测试管理员接口必须带 token"""
        with pytest.raises(HTTPException) as exc_info:
            require_admin_token(authorization=None)

        assert exc_info.value.status_code == 401


class TestEvaluationIdempotency:
    """测试评分幂等性"""

    @pytest.mark.asyncio
    async def test_submit_evaluation_idempotent(self, mock_session):
        """测试重复评分返回已有记录（幂等）"""
        conversation_id = uuid.uuid4()

        mock_conv = MagicMock()
        mock_conv.case_id = "TEST001"
        mock_conv.trace_id = "trace-1"

        conv_result = MagicMock()
        conv_result.scalar_one_or_none.return_value = mock_conv
        existing_result = MagicMock()
        existing_result.fetchone.return_value = (4, 82)
        mock_session.execute.side_effect = [conv_result, existing_result]

        result = await submit_evaluation(
            conversation_id=conversation_id,
            evaluation=EvaluationCreate(score=4),
            session=mock_session,
        )

        assert result.ok is True
        assert result.score == 4
        assert result.composite_score == 82


class TestEvaluationScoreMapping:
    """测试评分映射"""

    @pytest.mark.parametrize(
        "user_rating,expected_score",
        [
            (1, 0),
            (2, 25),
            (3, 50),
            (4, 75),
            (5, 100),
        ],
    )
    def test_user_rating_mapping(self, user_rating, expected_score):
        """测试用户评分到满意度分数的映射"""
        from app.services.quality_score import QualitySignals, compute_quality_score

        signals = QualitySignals(
            case_id="TEST",
            close_reason="user_command",
            session_duration_sec=300,
            message_count=5,
            repeat_question_count=0,
            user_rating=user_rating,
            has_sop=False,
            kb_chunks_count=0,
            kb_top_score=0.0,
        )

        result = compute_quality_score(signals)
        assert result.breakdown["user_rating"] == expected_score
