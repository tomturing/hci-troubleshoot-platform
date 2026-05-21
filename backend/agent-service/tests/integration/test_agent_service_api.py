"""
Agent Service API 集成测试
"""

import pytest
from app.main import app
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    """Test client fixture"""
    return TestClient(app)


@pytest.mark.integration
class TestAgentServiceHealth:
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

    def test_agent_health_endpoint(self, client):
        """测试 Agent 健康检查端点"""
        response = client.get("/v1/agent/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        # 注意：agent_router 可能为 None，取决于测试环境是否初始化
        # 所以我们不在这里断言它的值


@pytest.mark.integration
class TestAgentServiceMetrics:
    """指标测试"""

    def test_metrics_endpoint(self, client):
        """测试 Prometheus 指标端点"""
        response = client.get("/metrics")
        assert response.status_code == 200
        # 指标是纯文本格式，不是 JSON
        assert "text/plain" in response.headers["content-type"]


@pytest.mark.integration
class TestAgentServiceErrorHandling:
    """错误处理测试"""

    def test_nonexistent_endpoint(self, client):
        """测试不存在的端点返回 404"""
        response = client.get("/nonexistent")
        assert response.status_code == 404


@pytest.mark.integration
class TestAgentStreamEndpoint:
    """Agent 流端点测试（不依赖外部服务）"""

    def test_agent_stream_not_ready(self, client, mocker):
        """测试 Agent 未就绪时返回 503"""
        # Mock _agent_router 为 None
        import app.routes.agent as agent_routes
        original_router = agent_routes._agent_router
        agent_routes._agent_router = None

        try:
            # 不实际调用，因为需要 SSE 客户端
            # 这里只测试未就绪的错误路径
            request = {
                "assistant_type": "glm-5",
                "session_id": "test-session",
                "case_id": "TEST001",
                "user_id": "test-user",
                "messages": [{"role": "user", "content": "test"}]
            }
            response = client.post("/v1/agent/stream", json=request)
            # 当 _agent_router 为 None 时，应该返回 503
            assert response.status_code == 503
        finally:
            agent_routes._agent_router = original_router

    def test_interactive_response_not_ready(self, client):
        """测试交互响应未就绪时返回 503"""
        import app.routes.agent as agent_routes
        original_router = agent_routes._agent_router
        agent_routes._agent_router = None

        try:
            request = {
                "acp_session_id": "test-session",
                "request_id": "test-request",
                "outcome": "test-outcome"
            }
            response = client.post("/v1/agent/interactive-response", json=request)
            assert response.status_code == 503
        finally:
            agent_routes._agent_router = original_router
