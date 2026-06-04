"""
Unit Tests for capabilities proxy routes (tools, prompts, skills)
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


class TestCapabilitiesProxy(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    @patch("app.routes.capabilities.httpx.AsyncClient")
    def test_proxy_tools_get(self, mock_client_cls):
        """GET /api/v1/tools 返回上游数据"""
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = [{"id": 1, "tool_name": "acli_vm_list"}]
        mock_client.request.return_value = mock_response

        response = self.client.get("/api/v1/tools")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [{"id": 1, "tool_name": "acli_vm_list"}])
        args, kwargs = mock_client.request.call_args
        self.assertEqual(args[0], "GET")
        self.assertIn("/api/v1/tools", args[1])

    @patch("app.routes.capabilities.httpx.AsyncClient")
    def test_proxy_tools_post(self, mock_client_cls):
        """POST /api/v1/tools 创建工具"""
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.json.return_value = {"id": 2, "tool_name": "acli_host_list"}
        mock_client.request.return_value = mock_response

        payload = {"tool_name": "acli_host_list", "display_name": "主机列表"}
        response = self.client.post("/api/v1/tools", json=payload)

        self.assertEqual(response.status_code, 201)
        args, kwargs = mock_client.request.call_args
        self.assertEqual(args[0], "POST")
        self.assertEqual(kwargs["json"], payload)

    @patch("app.routes.capabilities.httpx.AsyncClient")
    def test_proxy_tools_upstream_error(self, mock_client_cls):
        """上游服务不可用时返回 503"""
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        import httpx

        # httpx.RequestError(request=...) 正确设置 request 属性
        mock_request = MagicMock()
        mock_request.url = "http://localhost/api/v1/tools"
        exc = httpx.RequestError("connection refused", request=mock_request)
        mock_client.request.side_effect = exc

        response = self.client.get("/api/v1/tools")

        self.assertEqual(response.status_code, 503)


class TestPromptsProxy(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    @patch("app.routes.capabilities.httpx.AsyncClient")
    def test_proxy_prompts_get(self, mock_client_cls):
        """GET /api/v1/prompts 返回上游数据"""
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = [{"id": 1, "name": "s0_v1", "stage": "S0"}]
        mock_client.request.return_value = mock_response

        response = self.client.get("/api/v1/prompts")

        self.assertEqual(response.status_code, 200)
        args, kwargs = mock_client.request.call_args
        self.assertEqual(args[0], "GET")
        self.assertIn("/api/v1/prompts", args[1])

    @patch("app.routes.capabilities.httpx.AsyncClient")
    def test_proxy_prompts_put(self, mock_client_cls):
        """PUT /api/v1/prompts/{id} 更新 Prompt"""
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"id": 1, "name": "s0_v2"}
        mock_client.request.return_value = mock_response

        payload = {"name": "s0_v2", "stage": "S0", "is_active": True}
        response = self.client.put("/api/v1/prompts/1", json=payload)

        self.assertEqual(response.status_code, 200)
        args, kwargs = mock_client.request.call_args
        self.assertEqual(args[0], "PUT")
        self.assertIn("/api/v1/prompts/1", args[1])

    @patch("app.routes.capabilities.httpx.AsyncClient")
    def test_proxy_prompts_delete(self, mock_client_cls):
        """DELETE /api/v1/prompts/{id} 删除 Prompt"""
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        mock_response = MagicMock()
        mock_response.status_code = 204
        mock_response.json.return_value = None
        mock_client.request.return_value = mock_response

        response = self.client.delete("/api/v1/prompts/1")

        self.assertEqual(response.status_code, 204)
        args, kwargs = mock_client.request.call_args
        self.assertEqual(args[0], "DELETE")
        self.assertIn("/api/v1/prompts/1", args[1])


class TestSkillsProxy(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    @patch("app.routes.capabilities.httpx.AsyncClient")
    def test_proxy_skills_get(self, mock_client_cls):
        """GET /api/v1/skills 返回上游数据"""
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = [{"id": 1, "skill_name": "disk_vendor_lifetime"}]
        mock_client.request.return_value = mock_response

        response = self.client.get("/api/v1/skills")

        self.assertEqual(response.status_code, 200)
        args, kwargs = mock_client.request.call_args
        self.assertEqual(args[0], "GET")
        self.assertIn("/api/v1/skills", args[1])
