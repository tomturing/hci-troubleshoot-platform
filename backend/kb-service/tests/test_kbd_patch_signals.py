from unittest.mock import AsyncMock, MagicMock

import app.routes.admin as admin_route
import pytest
from app.routes.admin import kbd_router, set_dependencies
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.mark.anyio
async def test_update_kbd_entry_signals_json_sql_cast():
    app = FastAPI()
    app.include_router(kbd_router)

    mock_db = MagicMock()
    mock_session = AsyncMock()
    mock_session.__aenter__.return_value = mock_session
    mock_db.async_session_factory.return_value = mock_session

    original_check_auth = admin_route._check_auth
    admin_route._check_auth = lambda req: None
    set_dependencies(mock_db)

    # Mock UPDATE result
    mock_update_row = MagicMock()
    mock_update_row.mappings.return_value.first.return_value = {"id": 1, "status": "draft"}
    mock_session.execute.return_value = mock_update_row

    client = TestClient(app)
    test_signals = [
        {
            "signal_category": "frontend",
            "acquirer": "qkv_task",
            "keyword": "启动虚拟机",
            "description": "在 HCI 控制台查看虚拟机任务详情",
            "produces": [{"name": "VM", "path": "vm"}],
        }
    ]

    response = client.patch(
        "/api/admin/kbd/1",
        json={"signals_json": test_signals},
        headers={"Authorization": "Bearer dev-internalapi-api-token-2026"},
    )

    admin_route._check_auth = original_check_auth

    assert response.status_code == 200

    # Verify the SQL executed uses CAST(:signals_json AS jsonb)
    executed_call = mock_session.execute.call_args
    sql_text = str(executed_call[0][0])
    assert "CAST(:signals_json AS jsonb)" in sql_text
    assert ":signals_json::jsonb" not in sql_text
