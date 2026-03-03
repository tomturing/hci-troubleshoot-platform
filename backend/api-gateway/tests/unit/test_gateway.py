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


if __name__ == "__main__":
    unittest.main()
