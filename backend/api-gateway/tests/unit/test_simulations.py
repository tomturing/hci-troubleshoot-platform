"""仿真控制面代理的真实工单绑定门禁。"""

import os
import sys
from unittest.mock import AsyncMock, patch

from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

_svc = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _svc not in sys.path:
    sys.path.insert(0, _svc)

from app.main import app


def test_simulation_test_run_binds_platform_case_before_runtime():
    client = TestClient(app)
    case_response = JSONResponse({"case_id": "Q2026081100001", "status": "created"}, status_code=201)
    runtime_response = JSONResponse({"test_run_id": "run-27123", "status": "preparing"}, status_code=200)
    with patch("app.routes.simulations._case_request", new=AsyncMock(return_value=case_response)) as case_request, patch(
        "app.routes.simulations._post", new=AsyncMock(return_value=runtime_response)
    ) as runtime_post:
        response = client.post(
            "/api/hci-sim/v1/simulations/test-runs",
            json={
                "kbd_id": "27123",
                "title": "KBD 27123 仿真验证",
                "description": "验证三条确定性关键信号",
                "connection": {"test_run_id": "run-27123"},
                "environment_context": {"test_run_id": "run-27123"},
            },
        )

    assert response.status_code == 200
    assert response.json()["case_id"] == "Q2026081100001"
    case_request.assert_awaited_once()
    runtime_payload = runtime_post.await_args.args[1]
    assert runtime_payload["case_id"] == "Q2026081100001"
    assert runtime_payload["environment_context"]["case_id"] == "Q2026081100001"

