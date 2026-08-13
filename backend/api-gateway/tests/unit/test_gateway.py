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

    @patch("app.routes.kb._internal_auth_headers")
    @patch("app.routes.kb.httpx.AsyncClient")
    def test_kbd_batch_jobs_proxy_forwards_query(self, mock_client_cls, mock_auth):
        """批量任务列表代理必须命中静态 batch 路由并透传分页参数。"""

        mock_auth.return_value = {"Authorization": "Bearer test"}
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"jobs": []}
        mock_client.request.return_value = mock_response

        response = self.client.get("/api/v1/kbd/batch/jobs?limit=5")

        self.assertEqual(response.status_code, 200)
        args, kwargs = mock_client.request.call_args
        self.assertEqual(args[0], "GET")
        self.assertIn("/api/admin/kbd/batch/jobs", args[1])
        self.assertEqual(kwargs.get("params"), {"limit": "5"})

    @patch("app.routes.kb._internal_auth_headers")
    @patch("app.routes.kb.httpx.AsyncClient")
    def test_kbd_batch_job_detail_proxy_is_not_captured_by_kbd_id(self, mock_client_cls, mock_auth):
        """批量任务详情不能被 /{kbd_id} 动态路由误解析。"""

        mock_auth.return_value = {"Authorization": "Bearer test"}
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"batch_id": "00000000-0000-0000-0000-000000000001", "items": []}
        mock_client.request.return_value = mock_response

        response = self.client.get("/api/v1/kbd/batch/jobs/00000000-0000-0000-0000-000000000001")

        self.assertEqual(response.status_code, 200)
        args, _ = mock_client.request.call_args
        self.assertIn("/api/admin/kbd/batch/jobs/00000000-0000-0000-0000-000000000001", args[1])

    @patch("app.routes.kb._internal_auth_headers")
    @patch("app.routes.kb.httpx.AsyncClient")
    def test_kbd_batch_job_retry_proxy_uses_static_batch_route(self, mock_client_cls, mock_auth):
        """重试入口必须透传到来源批次，不能被 KBD ID 动态路由捕获。"""

        mock_auth.return_value = {"Authorization": "Bearer test"}
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "batch_id": "00000000-0000-0000-0000-000000000002",
            "total": 3,
        }
        mock_client.request.return_value = mock_response

        response = self.client.post("/api/v1/kbd/batch/jobs/00000000-0000-0000-0000-000000000001/retry")

        self.assertEqual(response.status_code, 200)
        args, _ = mock_client.request.call_args
        self.assertEqual(args[0], "POST")
        self.assertIn(
            "/api/admin/kbd/batch/jobs/00000000-0000-0000-0000-000000000001/retry",
            args[1],
        )

    @patch("app.routes.kb._internal_auth_headers")
    @patch("app.routes.kb.httpx.AsyncClient")
    def test_kbd_batch_approve_proxy_forwards_body(self, mock_client_cls, mock_auth):
        """批量通过必须使用静态路由并原样透传审核快照。"""

        mock_auth.return_value = {"Authorization": "Bearer test"}
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        mock_response = MagicMock(status_code=200)
        mock_response.json.return_value = {"batch_id": "batch-approve", "total": 2}
        mock_client.request.return_value = mock_response
        payload = {
            "kbd_ids": [1, 2],
            "reviewer_id": 7,
            "review_note": None,
            "entries": {"1": {"lock_version": 3, "category_id": "虚拟机-017"}},
        }

        response = self.client.post("/api/v1/kbd/batch/approve", json=payload)

        self.assertEqual(response.status_code, 200)
        args, kwargs = mock_client.request.call_args
        self.assertEqual(args[0], "POST")
        self.assertIn("/api/admin/kbd/batch/approve", args[1])
        self.assertEqual(kwargs.get("json"), payload)

    @patch("app.routes.kb._internal_auth_headers")
    @patch("app.routes.kb.httpx.AsyncClient")
    def test_kbd_batch_reject_proxy_forwards_body(self, mock_client_cls, mock_auth):
        """批量拒绝必须透传审核人和不可为空的统一原因。"""

        mock_auth.return_value = {"Authorization": "Bearer test"}
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        mock_response = MagicMock(status_code=200)
        mock_response.json.return_value = {"batch_id": "batch-reject", "total": 2}
        mock_client.request.return_value = mock_response
        payload = {"kbd_ids": [1, 2], "reviewer_id": 8, "review_note": "证据不足"}

        response = self.client.post("/api/v1/kbd/batch/reject", json=payload)

        self.assertEqual(response.status_code, 200)
        args, kwargs = mock_client.request.call_args
        self.assertEqual(args[0], "POST")
        self.assertIn("/api/admin/kbd/batch/reject", args[1])
        self.assertEqual(kwargs.get("json"), payload)

    @patch("app.routes.kb._internal_auth_headers")
    @patch("app.routes.kb.httpx.AsyncClient")
    def test_convert_safe_pipeline_proxy_forwards_body_and_auth(self, mock_client_cls, mock_auth):
        """安全管道预览必须走管理接口并使用网关内部鉴权。"""
        mock_auth.return_value = {"Authorization": "Bearer test"}
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "command": "ps auxf",
            "extract": {"type": "text", "include": ["VM"], "column": 2},
            "removed_segments": [],
        }
        mock_client.request.return_value = mock_response

        payload = {"command": "ps auxf | grep VM | awk '{print $2}'"}
        response = self.client.post("/api/v1/kbd/tools/convert-safe-pipeline", json=payload)

        self.assertEqual(response.status_code, 200)
        mock_client.request.assert_called_once()
        args, kwargs = mock_client.request.call_args
        self.assertEqual(args[0], "POST")
        self.assertIn("/api/admin/kbd/tools/convert-safe-pipeline", args[1])
        self.assertEqual(kwargs.get("json"), payload)
        self.assertEqual(kwargs.get("headers"), {"Authorization": "Bearer test"})

    @patch("app.routes.kb._internal_auth_headers")
    @patch("app.routes.kb.httpx.AsyncClient")
    def test_qfk_command_preview_uses_agent_handler_endpoint(self, mock_client_cls, mock_auth):
        """管理端命令预览必须走 Agent Handler，不能在网关或浏览器自行拼接。"""
        mock_auth.return_value = {"Authorization": "Bearer test"}
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "tool": "qfk_system",
            "command": "acli --timeout 10 system ps",
        }
        mock_client.post.return_value = mock_response

        payload = {"signal": {"acquire": {"tool": "qfk_system", "args": {"command": "ps"}}}}
        response = self.client.post("/api/v1/kbd/command-preview", json=payload)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), mock_response.json.return_value)
        mock_client.post.assert_called_once()
        args, kwargs = mock_client.post.call_args
        self.assertIn("/internal/qfk-command-preview", args[0])
        self.assertEqual(kwargs["json"], payload)
        self.assertEqual(kwargs["headers"], {"Authorization": "Bearer test"})


if __name__ == "__main__":
    unittest.main()
