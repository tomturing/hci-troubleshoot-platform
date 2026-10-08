"""
测试 api-gateway bridge_logs 代理路由

覆盖（SRC L2 修复后：移除占位符 token 兜底，改为 require_user + HMAC 签名身份）：
  - payload 转发
  - 出口注入 HMAC 签名 X-Client-ID / X-Client-Signature（不再注入占位符 token）
  - 剥离用户可伪造的身份头
  - 现有 Authorization 透传
  - 上游错误透传
"""

import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

# 多服务共享 app/ 命名空间隔离
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


class TestBridgeLogsProxy(unittest.TestCase):
    """api-gateway /api/bridge-logs 代理路由测试"""

    def setUp(self):
        self.client = TestClient(app)

    @patch("app.routes.bridge_logs.httpx.AsyncClient")
    def test_proxy_bridge_logs_forwards_payload(self, mock_client_cls):
        """POST /api/bridge-logs 转发 payload 到 conversation-service"""
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        mock_response = MagicMock()
        mock_response.status_code = 202
        mock_response.json.return_value = {"ok": True, "accepted": 1, "skipped": 0}
        mock_client.post.return_value = mock_response

        response = self.client.post(
            "/api/bridge-logs",
            json={"logs": [{"case_id": "Q001", "event": "ssh.connected"}]},
        )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["ok"], True)

        # 验证 post 被调用
        args, kwargs = mock_client.post.call_args
        self.assertIn("/api/bridge-logs", args[0])
        self.assertIn("logs", kwargs["json"])

    @patch("app.routes.bridge_logs.httpx.AsyncClient")
    def test_proxy_bridge_logs_signs_client_identity(self, mock_client_cls):
        """匿名经网关回采：出口注入 HMAC 签名 X-Client-ID，不再注入占位符 token"""
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        mock_response = MagicMock()
        mock_response.status_code = 202
        mock_response.json.return_value = {"ok": True, "accepted": 1, "skipped": 0}
        mock_client.post.return_value = mock_response

        response = self.client.post(
            "/api/bridge-logs",
            json={"logs": [{"case_id": "Q001", "event": "test"}]},
        )

        self.assertEqual(response.status_code, 202)
        args, kwargs = mock_client.post.call_args
        headers = kwargs.get("headers", {})
        # 网关基于服务端 Cookie 的 client_id 重签注入归属凭据
        self.assertTrue(headers.get("X-Client-ID"))
        self.assertIn(".", headers.get("X-Client-Signature", ""))
        # 绝不注入历史占位符 token
        self.assertNotEqual(headers.get("Authorization"), "Bearer client-session-placeholder-token")
        self.assertIsNone(headers.get("Authorization"))

    @patch("app.routes.bridge_logs.httpx.AsyncClient")
    def test_proxy_bridge_logs_strips_forged_identity_headers(self, mock_client_cls):
        """用户自报的 X-Client-ID / X-Client-Signature 被剥离并以网关签名覆盖"""
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        mock_response = MagicMock()
        mock_response.status_code = 202
        mock_response.json.return_value = {"ok": True, "accepted": 1, "skipped": 0}
        mock_client.post.return_value = mock_response

        forged_id = "victim-client-id"
        response = self.client.post(
            "/api/bridge-logs",
            json={"logs": [{"case_id": "Q001", "event": "test"}]},
            headers={
                "X-Client-ID": forged_id,
                "X-Client-Signature": "0000.deadbeef",
            },
        )

        self.assertEqual(response.status_code, 202)
        args, kwargs = mock_client.post.call_args
        headers = kwargs.get("headers", {})
        # 出口身份必须来自网关签名，而非用户伪造值
        self.assertNotEqual(headers.get("X-Client-ID"), forged_id)
        self.assertNotEqual(headers.get("X-Client-Signature"), "0000.deadbeef")

    @patch("app.routes.bridge_logs.httpx.AsyncClient")
    def test_proxy_bridge_logs_forwards_existing_auth(self, mock_client_cls):
        """有 Authorization 时透传"""
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        mock_response = MagicMock()
        mock_response.status_code = 202
        mock_response.json.return_value = {"ok": True, "accepted": 1, "skipped": 0}
        mock_client.post.return_value = mock_response

        response = self.client.post(
            "/api/bridge-logs",
            json={"logs": [{"case_id": "Q001", "event": "test"}]},
            headers={"Authorization": "Bearer real-session-token"},
        )

        self.assertEqual(response.status_code, 202)
        # 验证透传了真实 token
        args, kwargs = mock_client.post.call_args
        headers = kwargs.get("headers", {})
        self.assertEqual(headers.get("Authorization"), "Bearer real-session-token")

    @patch("app.routes.bridge_logs.httpx.AsyncClient")
    def test_proxy_bridge_logs_upstream_error_passthrough(self, mock_client_cls):
        """上游非 200 时透传状态码"""
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.json.return_value = {"detail": "Database error"}
        mock_client.post.return_value = mock_response

        response = self.client.post(
            "/api/bridge-logs",
            json={"logs": [{"case_id": "Q001", "event": "test"}]},
        )

        self.assertEqual(response.status_code, 500)
        self.assertIn("detail", response.json())


if __name__ == "__main__":
    unittest.main()
