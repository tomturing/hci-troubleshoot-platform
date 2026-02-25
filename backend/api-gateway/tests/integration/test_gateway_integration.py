"""
API Gateway - 集成测试
"""

import pytest
import httpx
from httpx import ASGITransport
import uuid
import sys
import os
from unittest.mock import patch, MagicMock, AsyncMock

# 确保能正确导入并加载 .env
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../..')))

from app.main import app

@pytest.fixture
def test_app():
    return app

class TestGatewayIntegration:
    BASE_URL = "http://testserver"

    @pytest.mark.asyncio
    async def test_health_check(self, test_app):
        """测试网关健康检查和TraceID中间件"""
        async with test_app.router.lifespan_context(test_app):
            async with httpx.AsyncClient(
                transport=ASGITransport(app=test_app),
                base_url=self.BASE_URL
            ) as client:
                response = await client.get("/health")
                assert response.status_code == 200
                data = response.json()
                assert data["status"] == "healthy"
                
                # 验证TraceID中间件是否成功注入了响应头
                assert "X-Trace-ID" in response.headers
                assert len(response.headers["X-Trace-ID"]) > 0

    @pytest.mark.asyncio
    async def test_case_proxy(self, test_app):
        """测试工单代理路由"""
        with patch('app.routes.cases.proxy_request') as mock_proxy:
            # 构造 httpx Response
            mock_response = httpx.Response(201, json={"case_id": "Q123", "status": "created"})
            mock_proxy.return_value = mock_response

            async with test_app.router.lifespan_context(test_app):
                async with httpx.AsyncClient(
                    transport=ASGITransport(app=test_app),
                    base_url=self.BASE_URL
                ) as client:
                    payload = {
                        "client_id": "test-client",
                        "title": "Test Integration",
                        "description": "Proxy test"
                    }
                    response = await client.post("/api/cases/", json=payload)
                    
                    assert response.status_code == 201
                    data = response.json()
                    assert data["case_id"] == "Q123"
                    assert "X-Trace-ID" in response.headers
                    
                    # 检查是否调用了代理
                    mock_proxy.assert_called_once()
                    args, kwargs = mock_proxy.call_args
                    assert args[0] == "POST"
                    assert args[1] == "/"
                    assert args[2] == payload

    @pytest.mark.asyncio
    async def test_conversation_proxy(self, test_app):
        """测试对话代理路由"""
        with patch('app.routes.conversations.proxy_request') as mock_proxy:
            mock_response = httpx.Response(200, json=[{"role": "user", "content": "hi"}])
            mock_proxy.return_value = mock_response

            async with test_app.router.lifespan_context(test_app):
                async with httpx.AsyncClient(
                    transport=ASGITransport(app=test_app),
                    base_url=self.BASE_URL
                ) as client:
                    conv_id = "test-conv-id"
                    response = await client.get(f"/api/conversations/{conv_id}/messages")
                    
                    assert response.status_code == 200
                    data = response.json()
                    assert len(data) == 1
                    assert data[0]["role"] == "user"
