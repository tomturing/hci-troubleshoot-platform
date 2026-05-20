"""
Eval Service API 集成测试
"""

import uuid

import pytest
from app.main import app
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    """Test client fixture"""
    return TestClient(app)


@pytest.mark.integration
class TestEvalServiceHealth:
    """健康检查测试"""

    def test_health_live(self, client):
        """测试存活探针"""
        response = client.get("/health/live")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "alive"

    def test_health_ready(self, client):
        """测试就绪探针"""
        response = client.get("/health/ready")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ready"


@pytest.mark.integration
class TestEvalServiceMetrics:
    """指标测试"""

    def test_metrics_endpoint(self, client):
        """测试 Prometheus 指标端点"""
        response = client.get("/metrics")
        assert response.status_code == 200
        assert "text/plain" in response.headers["content-type"]


@pytest.mark.integration
class TestEvalServiceErrorHandling:
    """错误处理测试"""

    def test_nonexistent_endpoint(self, client):
        """测试不存在的端点返回 404"""
        response = client.get("/nonexistent")
        assert response.status_code == 404


@pytest.mark.integration
class TestEvalServiceEndpoints:
    """评估服务端点测试（不依赖数据库）"""

    def test_admin_stats_unauthorized(self, client):
        """测试管理员接口未授权"""
        # 没有 token 应该直接返回 401，避免放宽为 422 掩盖认证回归
        response = client.get("/api/admin/quality/stats")
        assert response.status_code == 401
        data = response.json()
        assert "detail" in data
        assert data["detail"]

    def test_admin_stats_wrong_token(self, client):
        """测试管理员接口使用错误的 token"""
        response = client.get(
            "/api/admin/quality/stats",
            headers={"Authorization": "Bearer wrong-token"}
        )
        # 错误 token 应该返回 403，不应出现 500
        assert response.status_code == 403

    def test_get_evaluation_db_not_initialized(self, client):
        """测试数据库未初始化时返回 500"""
        import app.routes.evaluate as eval_routes
        original_db = eval_routes.database_manager
        eval_routes.database_manager = None

        try:
            fake_conv_id = str(uuid.uuid4())
            response = client.get(f"/api/conversations/{fake_conv_id}/evaluation")
            # 数据库未初始化应该返回 500
            assert response.status_code == 500
            data = response.json()
            assert "detail" in data
        finally:
            eval_routes.database_manager = original_db

    def test_submit_evaluation_invalid_score(self, client):
        """测试提交评价时的参数验证（使用 dependency override 隔离数据库）"""
        from unittest.mock import AsyncMock

        from app.routes.evaluate import get_db_session

        # 正确 Mock 数据库会话：fetchone 应在 Result 对象上
        mock_result = AsyncMock()
        mock_result.fetchone.return_value = None
        mock_result.scalar.return_value = 0

        mock_session = AsyncMock()
        mock_session.execute.return_value = mock_result
        mock_session.commit = AsyncMock()

        async def mock_get_db_session():
            yield mock_session

        # 使用 dependency override
        app.dependency_overrides[get_db_session] = mock_get_db_session

        try:
            fake_conv_id = str(uuid.uuid4())

            # 无效评分（大于 5）应该返回 422
            response = client.post(
                f"/api/conversations/{fake_conv_id}/evaluate",
                json={"score": 10}
            )
            assert response.status_code == 422

            # 无效评分（小于 1）应该返回 422
            response = client.post(
                f"/api/conversations/{fake_conv_id}/evaluate",
                json={"score": 0}
            )
            assert response.status_code == 422

            # 缺少评分字段应该返回 422
            response = client.post(
                f"/api/conversations/{fake_conv_id}/evaluate",
                json={}
            )
            assert response.status_code == 422
        finally:
            # 清理 dependency override
            app.dependency_overrides.clear()
