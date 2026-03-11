"""
测试 Evaluation API - 用户评分接口
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession


class TestEvaluationAPI:
    """测试评分 API 接口"""

    @pytest.fixture
    def mock_session(self):
        """创建模拟的数据库会话"""
        session = AsyncMock(spec=AsyncSession)
        return session

    def test_submit_evaluation_success(self, client: TestClient, mock_session):
        """测试提交评分成功"""
        conversation_id = str(uuid.uuid4())

        # 模拟数据库返回
        with patch(
            "app.routes.evaluate.get_db_session",
            return_value=mock_session,
        ), patch(
            "app.services.quality_score.QualityScoreService.update_user_rating",
            return_value=82,
        ):
            response = client.post(
                f"/api/conversations/{conversation_id}/evaluate",
                json={"score": 4},
            )

            assert response.status_code == 200
            data = response.json()
            assert data["ok"] is True
            assert data["score"] == 4
            assert data["composite_score"] == 82
            assert "message" in data

    def test_submit_evaluation_invalid_score(self, client: TestClient):
        """测试提交无效评分"""
        conversation_id = str(uuid.uuid4())

        response = client.post(
            f"/api/conversations/{conversation_id}/evaluate",
            json={"score": 6},  # 无效评分：超过 5
        )

        assert response.status_code == 422  # Pydantic validation error

    def test_submit_evaluation_conversation_not_found(self, client: TestClient):
        """测试对话不存在"""
        conversation_id = str(uuid.uuid4())

        # 模拟数据库返回 None（对话不存在）
        with patch(
            "app.routes.evaluate.get_db_session"
        ) as mock_get_session:
            mock_session = AsyncMock()
            mock_session.execute.return_value.scalar_one_or_none.return_value = None
            mock_get_session.return_value.__aiter__ = AsyncMock(return_value=[mock_session])

            response = client.post(
                f"/api/conversations/{conversation_id}/evaluate",
                json={"score": 4},
            )

            assert response.status_code == 404

    def test_get_quality_stats_admin(self, client: TestClient):
        """测试获取质量统计（Admin）"""
        with patch(
            "app.services.quality_score.QualityStatsService.get_quality_stats",
            return_value={
                "period_days": 7,
                "average_composite_score": 75.5,
                "total_evaluated_cases": 100,
                "low_score_cases": 5,
                "low_score_rate": 5.0,
                "close_reason_distribution": {"user_command": 80, "timeout": 15, "abandon": 5},
                "user_rating_distribution": {"4": 30, "5": 40, "unrated": 30},
            },
        ):
            response = client.get("/api/admin/quality/stats?days=7")

            assert response.status_code == 200
            data = response.json()
            assert "average_composite_score" in data
            assert "close_reason_distribution" in data

    def test_get_low_score_cases_admin(self, client: TestClient):
        """测试获取低分工件列表（Admin）"""
        with patch(
            "app.services.quality_score.QualityStatsService.get_low_score_cases",
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
            },
        ):
            response = client.get("/api/admin/quality/cases?min_score=0&max_score=39")

            assert response.status_code == 200
            data = response.json()
            assert "items" in data
            assert data["total"] == 1


class TestEvaluationIdempotency:
    """测试评分幂等性"""

    def test_submit_evaluation_idempotent(self, client: TestClient):
        """测试重复评分返回已有记录（幂等）"""
        conversation_id = str(uuid.uuid4())

        with patch(
            "app.routes.evaluate.get_db_session"
        ) as mock_get_session, patch(
            "app.routes.evaluate.QualityScoreService"
        ) as mock_service_class:
            # 模拟已有评分记录
            mock_session = AsyncMock()
            mock_result = MagicMock()
            mock_result.fetchone.return_value = (4, 82)  # 已有评分和综合分
            mock_session.execute.return_value = mock_result
            mock_get_session.return_value.__aiter__ = AsyncMock(return_value=[mock_session])

            # 模拟 conversation 存在
            mock_conv = MagicMock()
            mock_conv.case_id = "TEST001"
            mock_conv.scalar_one_or_none.return_value = mock_conv

            response = client.post(
                f"/api/conversations/{conversation_id}/evaluate",
                json={"score": 4},
            )

            # 幂等：应该返回 200，而不是 409
            assert response.status_code == 200
            data = response.json()
            assert data["ok"] is True
            assert data["score"] == 4
            assert data["composite_score"] == 82


class TestEvaluationScoreMapping:
    """测试评分映射"""

    @pytest.mark.parametrize(
        "user_rating,expected_score",
        [
            (1, 0),   # 1星 → 0分
            (2, 25),  # 2星 → 25分
            (3, 50),  # 3星 → 50分
            (4, 75),  # 4星 → 75分
            (5, 100), # 5星 → 100分
        ],
    )
    def test_user_rating_mapping(self, user_rating, expected_score):
        """测试用户评分到满意度分数的映射"""
        from app.services.quality_score import compute_quality_score, QualitySignals

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
