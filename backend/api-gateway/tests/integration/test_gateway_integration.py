"""
API Gateway - 集成测试
"""

import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from httpx import ASGITransport

pytestmark = pytest.mark.integration

# 多服务共享 app/ 命名空间，仅在 app 指向错误服务时清除重载
_svc = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_expect = os.path.normpath(os.path.join(_svc, "app"))
_actual = os.path.normpath(getattr(sys.modules.get("app"), "__path__", [""])[0]) if "app" in sys.modules else ""
if _expect != _actual:
    for _k in list(sys.modules):
        if _k == "app" or _k.startswith("app."):
            del sys.modules[_k]
    if _svc in sys.path:
        sys.path.remove(_svc)
    sys.path.insert(0, _svc)

from app.main import app


@pytest.fixture
def test_app():
    return app

class TestGatewayIntegration:
    BASE_URL = "http://testserver"

    @pytest.mark.asyncio
    async def test_health_check(self, test_app):
        """测试网关健康检查和TraceID中间件"""
        async with test_app.router.lifespan_context(test_app), httpx.AsyncClient(
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

            async with test_app.router.lifespan_context(test_app), httpx.AsyncClient(
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

            async with test_app.router.lifespan_context(test_app), httpx.AsyncClient(
                transport=ASGITransport(app=test_app),
                base_url=self.BASE_URL
            ) as client:
                conv_id = "test-conv-id"
                response = await client.get(f"/api/conversations/{conv_id}/messages")

                assert response.status_code == 200
                data = response.json()
                assert len(data) == 1
                assert data[0]["role"] == "user"

    @pytest.mark.asyncio
    async def test_close_case_success(self, test_app):
        """测试关闭工单成功并释放 Pod"""
        with patch('app.routes.cases.proxy_request') as mock_proxy, \
             patch('httpx.AsyncClient.post') as mock_scheduler_post:
            # 模拟工单关闭成功
            mock_case_response = httpx.Response(200, json={"case_id": "Q123", "status": "closed"})
            mock_proxy.return_value = mock_case_response

            # 模拟 scheduler 释放成功
            mock_scheduler_resp = AsyncMock()
            mock_scheduler_resp.status_code = 200
            mock_scheduler_post.return_value = mock_scheduler_resp

            async with test_app.router.lifespan_context(test_app), httpx.AsyncClient(
                transport=ASGITransport(app=test_app),
                base_url=self.BASE_URL
            ) as client:
                response = await client.put("/api/cases/Q123/close")

                assert response.status_code == 200
                data = response.json()
                assert data["case_id"] == "Q123"
                assert data["status"] == "closed"

                # 验证调用了 proxy_request 关闭工单
                mock_proxy.assert_called_once()
                args, _ = mock_proxy.call_args
                assert args[0] == "PUT"
                assert args[1] == "/Q123/close"

                # 验证调用了 scheduler 释放 Pod
                mock_scheduler_post.assert_called_once()

    @pytest.mark.asyncio
    async def test_close_case_not_found(self, test_app):
        """测试关闭不存在的工单"""
        with patch('app.routes.cases.proxy_request') as mock_proxy:
            # 模拟工单不存在
            mock_case_response = httpx.Response(404, json={"detail": "Case not found"})
            mock_proxy.return_value = mock_case_response

            async with test_app.router.lifespan_context(test_app), httpx.AsyncClient(
                transport=ASGITransport(app=test_app),
                base_url=self.BASE_URL
            ) as client:
                response = await client.put("/api/cases/non-existent/close")

                assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_close_case_scheduler_failure(self, test_app):
        """测试关闭工单成功但 scheduler 释放失败（不应阻断）"""
        with patch('app.routes.cases.proxy_request') as mock_proxy, \
             patch('httpx.AsyncClient.post') as mock_scheduler_post:
            # 模拟工单关闭成功
            mock_case_response = httpx.Response(200, json={"case_id": "Q123", "status": "closed"})
            mock_proxy.return_value = mock_case_response

            # 模拟 scheduler 释放失败
            mock_scheduler_resp = AsyncMock()
            mock_scheduler_resp.status_code = 500
            mock_scheduler_post.return_value = mock_scheduler_resp

            async with test_app.router.lifespan_context(test_app), httpx.AsyncClient(
                transport=ASGITransport(app=test_app),
                base_url=self.BASE_URL
            ) as client:
                response = await client.put("/api/cases/Q123/close")

                # 即使 scheduler 失败，工单关闭也应成功
                assert response.status_code == 200
                data = response.json()
                assert data["case_id"] == "Q123"
                assert data["status"] == "closed"

                # 验证尝试调用了 scheduler
                mock_scheduler_post.assert_called_once()

    @pytest.mark.asyncio
    async def test_close_case_scheduler_exception(self, test_app):
        """测试关闭工单成功但 scheduler 调用抛出异常（不应阻断）"""
        with patch('app.routes.cases.proxy_request') as mock_proxy, \
             patch('httpx.AsyncClient.post') as mock_scheduler_post, \
             patch('app.routes.cases.POD_RELEASE_FAILURES_TOTAL.labels') as mock_metric:
            # 模拟工单关闭成功
            mock_case_response = httpx.Response(200, json={"case_id": "Q123", "status": "closed"})
            mock_proxy.return_value = mock_case_response

            # 模拟 scheduler 调用抛出异常
            mock_scheduler_post.side_effect = Exception("Scheduler unavailable")

            # 模拟 metric（Prometheus metric.inc() 是同步调用，用 MagicMock）
            mock_metric_instance = MagicMock()
            mock_metric.return_value = mock_metric_instance

            async with test_app.router.lifespan_context(test_app), httpx.AsyncClient(
                transport=ASGITransport(app=test_app),
                base_url=self.BASE_URL
            ) as client:
                response = await client.put("/api/cases/Q123/close")

                # 即使 scheduler 异常，工单关闭也应成功
                assert response.status_code == 200
                data = response.json()
                assert data["case_id"] == "Q123"
                assert data["status"] == "closed"

                # 验证 metric 被记录
                mock_metric.assert_called_once()
                mock_metric_instance.inc.assert_called_once()
