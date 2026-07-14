"""
Unit Tests for API Gateway
"""

import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

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


class TestGateway(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    @patch("app.routes.cases.httpx.AsyncClient")
    def test_create_case_proxy(self, mock_client_cls):
        """Test creating case is proxied to case-service"""
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.json.return_value = {"case_id": "123", "status": "created"}
        mock_client.request.return_value = mock_response

        payload = {"title": "Test Case", "description": "Test", "user_id": "u1", "client_id": "c1"}
        response = self.client.post("/api/cases/", json=payload)

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json(), {"case_id": "123", "status": "created"})

        # Verify proxy call
        mock_client.request.assert_called_once()
        args, kwargs = mock_client.request.call_args
        self.assertEqual(args[0], "POST")
        self.assertIn("/api/cases/", args[1])
        self.assertEqual(kwargs["json"], payload)

    @patch("app.routes.cases.httpx.AsyncClient")
    def test_get_case_proxy(self, mock_client_cls):
        """Test getting case is proxied"""
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"case_id": "123", "title": "Test Case"}
        mock_client.request.return_value = mock_response

        response = self.client.get("/api/cases/123")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"case_id": "123", "title": "Test Case"})

        mock_client.request.assert_called_once()
        args, _ = mock_client.request.call_args
        self.assertIn("/api/cases/123", args[1])


    @patch("app.routes.kb._internal_auth_headers")
    @patch("app.routes.kb.httpx.AsyncClient")
    def test_reanalyze_single_image_proxy_forwards_query(self, mock_client_cls, mock_auth):
        """单张重新识图代理必须透传 query 参数（如 ?sync=true）

        回归测试：此前代理调用 _kbd_proxy 时漏传 params，导致 kb-service
        收不到 sync 而走异步 202，前端收到 {job_id,status:"pending"} 后
        data.screenshot_type 为 undefined，弹出“识图完成：undefined”。
        """
        mock_auth.return_value = {"Authorization": "Bearer test"}
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"success": True, "screenshot_type": "任务截图", "message": "识图完成"}
        mock_client.request.return_value = mock_response

        response = self.client.post("/api/v1/kbd/1/reanalyze-image/0?sync=true")

        self.assertEqual(response.status_code, 200)
        mock_client.request.assert_called_once()
        args, kwargs = mock_client.request.call_args
        self.assertEqual(args[0], "POST")
        self.assertIn("/api/admin/kbd/1/reanalyze-image/0", args[1])
        # 核心断言：query 参数被透传，sync=true 才能到达 kb-service
        self.assertEqual(kwargs.get("params"), {"sync": "true"})

    @patch("app.routes.kb._internal_auth_headers")
    @patch("app.routes.kb.httpx.AsyncClient")
    def test_reanalyze_images_proxy_forwards_query(self, mock_client_cls, mock_auth):
        """批量重新识图代理同样必须透传 query 参数。"""
        mock_auth.return_value = {"Authorization": "Bearer test"}
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"success": True, "total": 1, "done": 1, "failed": 0}
        mock_client.request.return_value = mock_response

        response = self.client.post("/api/v1/kbd/1/reanalyze-images?sync=true")

        self.assertEqual(response.status_code, 200)
        mock_client.request.assert_called_once()
        args, kwargs = mock_client.request.call_args
        self.assertIn("/api/admin/kbd/1/reanalyze-images", args[1])
        self.assertEqual(kwargs.get("params"), {"sync": "true"})


if __name__ == "__main__":
    unittest.main()
