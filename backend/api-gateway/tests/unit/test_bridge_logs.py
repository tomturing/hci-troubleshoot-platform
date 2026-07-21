"""
测试 api-gateway bridge_logs 代理路由

覆盖：
  - payload 转发
  - 占位符 token 注入
  - 现有 Authorization 透传
  - 上游错误透传
"""

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient


class TestBridgeLogsProxy(unittest.TestCase):
    """api-gateway /api/bridge-logs 代理路由测试"""

    def setUp(self):
        """每个测试前 mock httpx.AsyncClient"""
        self.mock_client = MagicMock()
        self.mock_response = MagicMock()
        self.mock_response.json.return_value = {"ok": True, "accepted": 1, "skipped": 0}
        self.mock_response.status_code = 202

        # Mock async context manager
        async_cm = AsyncMock()
        async_cm.__aenter__.return_value = self.mock_response
        async_cm.__aexit__.return_value = None

        self.mock_client.post.return_value = async_cm

        # Mock async context manager for client
        self.mock_async_client = AsyncMock()
        self.mock_async_client.__aenter__.return_value = self.mock_client
        self.mock_async_client.__aexit__.return_value = None

    def test_proxy_bridge_logs_forwards_payload(self):
        """POST /api/bridge-logs 转发 payload 到 conversation-service"""
        from app.main import app

        with patch("app.routes.bridge_logs.httpx.AsyncClient") as mock_async_client_cls:
            mock_async_client_cls.return_value = self.mock_async_client

            client = TestClient(app)
            response = client.post(
                "/api/bridge-logs",
                json={"logs": [{"case_id": "Q001", "event": "ssh.connected"}]},
            )

            self.assertEqual(response.status_code, 202)
            self.assertEqual(response.json()["ok"], True)

            # 验证 post 被调用
            self.mock_client.post.assert_called_once()
            call_args = self.mock_client.post.call_args
            self.assertIn("logs", call_args.kwargs["json"])

    def test_proxy_bridge_logs_injects_placeholder_token(self):
        """无 Authorization 时注入占位符 token"""
        from app.main import app

        with patch("app.routes.bridge_logs.httpx.AsyncClient") as mock_async_client_cls:
            mock_async_client_cls.return_value = self.mock_async_client

            client = TestClient(app)
            response = client.post(
                "/api/bridge-logs",
                json={"logs": [{"case_id": "Q001", "event": "test"}]},
            )

            # 验证注入了占位符 token
            call_args = self.mock_client.post.call_args
            headers = call_args.kwargs["headers"]
            self.assertEqual(headers["Authorization"], "Bearer client-session-placeholder-token")

    def test_proxy_bridge_logs_forwards_existing_auth(self):
        """有 Authorization 时透传"""
        from app.main import app

        with patch("app.routes.bridge_logs.httpx.AsyncClient") as mock_async_client_cls:
            mock_async_client_cls.return_value = self.mock_async_client

            client = TestClient(app)
            response = client.post(
                "/api/bridge-logs",
                json={"logs": [{"case_id": "Q001", "event": "test"}]},
                headers={"Authorization": "Bearer real-session-token"},
            )

            # 验证透传了真实 token
            call_args = self.mock_client.post.call_args
            headers = call_args.kwargs["headers"]
            self.assertEqual(headers["Authorization"], "Bearer real-session-token")

    def test_proxy_bridge_logs_upstream_error_passthrough(self):
        """上游非 200 时透传状态码"""
        from app.main import app

        # Mock 500 response
        self.mock_response.status_code = 500
        self.mock_response.json.return_value = {"detail": "Database error"}

        with patch("app.routes.bridge_logs.httpx.AsyncClient") as mock_async_client_cls:
            mock_async_client_cls.return_value = self.mock_async_client

            client = TestClient(app)
            response = client.post(
                "/api/bridge-logs",
                json={"logs": [{"case_id": "Q001", "event": "test"}]},
            )

            self.assertEqual(response.status_code, 500)
            self.assertIn("detail", response.json())


if __name__ == "__main__":
    unittest.main()
