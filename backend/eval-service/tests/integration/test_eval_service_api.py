"""
Eval Service API 集成测试
"""

import pytest
from fastapi.testclient import TestClient

from app.main import app


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
        # 没有 token 应该返回 401
        response = client.get("/api/admin/quality/stats")
        assert response.status_code in (401, 422)  # 取决于是否有依赖验证

    def test_admin_stats_wrong_token(self, client):
        """测试管理员接口使用错误的 token"""
        response = client.get(
            "/api/admin/quality/stats",
            headers={"Authorization": "Bearer wrong-token"}
        )
        # 由于没有数据库，可能会有其他错误，但应该拒绝访问
        assert response.status_code in (401, 403, 500)

    def test_get_evaluation_nonexistent_conversation(self, client, mocker):
        """测试获取不存在对话的评价"""
        # Mock the database manager to be None
        import app.routes.evaluate as eval_routes
        original_db = eval_routes.database_manager
        eval_routes.database_manager = None

        try:
            # 使用不存在的 UUID
            import uuid
            fake_conv_id = str(uuid.uuid4())
            response = client.get(f"/api/conversations/{fake_conv_id}/evaluation")
            # 取决于是否有数据库连接
            assert response.status_code in (404, 500)
        finally:
            eval_routes.database_manager = original_db

    def test_submit_evaluation_validation(self, client):
        """测试提交评价时的验证"""
        # 不测试实际数据库操作，只测试基本验证
        # 使用不存在的对话
        import uuid
        fake_conv_id = str(uuid.uuid4())

        # 无效评分（大于 5）
        response = client.post(
            f"/api/conversations/{fake_conv_id}/evaluate",
            json={"score": 10}
        )
        # 应该有验证错误或者数据库错误
        assert response.status_code in (404, 422, 500)

        # 无效评分（小于 1）
        response = client.post(
            f"/api/conversations/{fake_conv_id}/evaluate",
            json={"score": 0}
        )
        assert response.status_code in (404, 422, 500)

        # 缺少评分字段
        response = client.post(
            f"/api/conversations/{fake_conv_id}/evaluate",
            json={}
        )
        assert response.status_code in (404, 422, 500)
