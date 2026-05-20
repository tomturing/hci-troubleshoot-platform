"""
AI Client 单元测试
"""

import pytest

from app.services.ai_client import AIAssistantRegistry, OpenClawAssistant, create_openclaw_client


class TestOpenClawAssistant:
    """OpenClawAssistant 测试"""

    def test_create_client(self):
        """测试创建客户端"""
        client = OpenClawAssistant(
            base_url="http://localhost:8000",
            api_key="test-key",
            default_model="glm-4-flash",
            assistant_type="glm-4-flash"
        )
        assert client.base_url == "http://localhost:8000"
        assert client.gateway_token == "test-key"
        assert client.default_model == "glm-4-flash"
        assert client.assistant_type == "glm-4-flash"

    def test_is_internal_gateway_endpoint(self):
        """测试内部网关端点识别"""
        # 内部端点应该返回 True
        assert OpenClawAssistant._is_internal_gateway_endpoint("http://localhost:8000") is True
        assert OpenClawAssistant._is_internal_gateway_endpoint("http://127.0.0.1:8000") is True
        assert OpenClawAssistant._is_internal_gateway_endpoint("http://openclaw:8000") is True
        assert OpenClawAssistant._is_internal_gateway_endpoint("http://openclaw.svc:8000") is True
        assert OpenClawAssistant._is_internal_gateway_endpoint("http://openclaw.svc.cluster.local:8000") is True
        assert OpenClawAssistant._is_internal_gateway_endpoint("http://192.168.1.100:8000") is True
        assert OpenClawAssistant._is_internal_gateway_endpoint("http://10.0.0.1:8000") is True

        # 外部端点应该返回 False
        assert OpenClawAssistant._is_internal_gateway_endpoint("https://open.bigmodel.cn") is False
        assert OpenClawAssistant._is_internal_gateway_endpoint("http://8.8.8.8:8000") is False

    def test_resolve_auth_token(self):
        """测试认证 token 解析"""
        client = OpenClawAssistant(
            base_url="http://localhost:8000",
            api_key="gateway-token",
            provider_api_key="provider-key"
        )
        # 内部端点使用 gateway token
        assert client._resolve_auth_token("http://localhost:8000") == "gateway-token"
        # 外部端点使用 provider key
        assert client._resolve_auth_token("https://api.example.com") == "provider-key"

    def test_is_retriable_stream_error(self):
        """测试可重试错误识别"""
        # 可重试的错误
        assert OpenClawAssistant._is_retriable_stream_error(Exception("Incomplete chunked read")) is True
        assert OpenClawAssistant._is_retriable_stream_error(Exception("Peer closed connection")) is True
        assert OpenClawAssistant._is_retriable_stream_error(Exception("Read timeout")) is True
        # 不可重试的错误
        assert OpenClawAssistant._is_retriable_stream_error(Exception("Invalid request")) is False


class TestAIAssistantRegistry:
    """AIAssistantRegistry 测试"""

    @pytest.fixture
    def registry(self):
        """注册表 fixture"""
        return AIAssistantRegistry()

    @pytest.fixture
    def mock_client(self, mocker):
        """Mock AI client"""
        client = mocker.Mock()
        client.check_health = mocker.AsyncMock(return_value=True)
        client.close = mocker.AsyncMock()
        return client

    def test_register_client(self, registry, mock_client):
        """测试注册客户端"""
        registry.register("glm-4-flash", mock_client)
        assert registry.get_client("glm-4-flash") is mock_client
        assert "glm-4-flash" in registry.list_types()

    def test_register_default_client(self, registry, mock_client):
        """测试注册默认客户端"""
        client1 = mock_client
        client2 = mock_client
        registry.register("glm-4-flash", client1)
        registry.register("glm-4", client2, is_default=True)
        assert registry.get_default_type() == "glm-4"
        assert registry.get_client() is client2  # 不传参数获取默认

    def test_set_default_type(self, registry, mock_client):
        """测试设置默认类型"""
        registry.register("glm-4-flash", mock_client)
        registry.register("glm-4", mock_client)
        registry.set_default_type("glm-4")
        assert registry.get_default_type() == "glm-4"

    def test_get_nonexistent_client(self, registry):
        """测试获取不存在的客户端"""
        assert registry.get_client("nonexistent") is None

    def test_list_types(self, registry, mock_client):
        """测试列出所有类型"""
        registry.register("glm-4-flash", mock_client)
        registry.register("glm-4", mock_client)
        types = registry.list_types()
        assert len(types) == 2
        assert "glm-4-flash" in types
        assert "glm-4" in types

    @pytest.mark.asyncio
    async def test_health_check_all(self, registry, mock_client):
        """测试健康检查"""
        healthy_client = mock_client
        healthy_client.check_health.return_value = True
        unhealthy_client = mock_client
        unhealthy_client.check_health.return_value = False
        registry.register("healthy", healthy_client)
        registry.register("unhealthy", unhealthy_client)
        results = await registry.health_check_all()
        assert results["healthy"] is True
        assert results["unhealthy"] is False

    @pytest.mark.asyncio
    async def test_close_all(self, registry, mock_client):
        """测试关闭所有客户端"""
        registry.register("glm-4-flash", mock_client)
        registry.register("glm-4", mock_client)
        await registry.close_all()
        assert mock_client.close.call_count == 2
        assert len(registry.list_types()) == 0


class TestCreateOpenClawClient:
    """工厂函数测试"""

    def test_create_openclaw_client(self):
        """测试创建 OpenClaw 客户端"""
        client = create_openclaw_client(
            base_url="http://localhost:8000",
            api_key="test-key",
            default_model="glm-4-flash",
            assistant_type="glm-4-flash"
        )
        assert isinstance(client, OpenClawAssistant)
        assert client.base_url == "http://localhost:8000"
