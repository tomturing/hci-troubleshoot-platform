"""KBD Capability 契约与 Agent 运行时发现合并测试。"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from app.routes import kb
from starlette.requests import Request


def _request() -> Request:
    return Request({"type": "http", "method": "GET", "path": "/api/v1/kbd/capabilities", "headers": []})


def _contract_response() -> MagicMock:
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {
        "schema_version": 1,
        "capabilities": [
            {
                "capability_id": "qkv_task",
                "runtime_status": "unknown",
                "verification_status": "contract_only",
            },
            {
                "capability_id": "qfk_system",
                "runtime_status": "unknown",
                "verification_status": "contract_only",
            },
        ],
    }
    return response


@pytest.mark.asyncio
async def test_kbd_capabilities_merges_agent_runtime_discovery():
    """共享参数契约必须与当前 Agent Pod 的真实运行时状态按稳定 ID 合并。"""

    runtime_response = MagicMock()
    runtime_response.status_code = 200
    runtime_response.json.return_value = {
        "service": "agent-service",
        "capabilities": [
            {
                "capability_id": "qkv_task",
                "runtime_status": "available",
                "implemented": True,
                "deployed": True,
                "validator_ready": True,
                "executor_ready": True,
                "usable": True,
            }
        ],
    }
    runtime_client = AsyncMock()
    runtime_client.get.return_value = runtime_response
    client_context = MagicMock()
    client_context.__aenter__ = AsyncMock(return_value=runtime_client)
    client_context.__aexit__ = AsyncMock(return_value=False)

    with (
        patch.object(kb, "_internal_auth_headers", return_value={"Authorization": "Bearer test"}),
        patch.object(kb, "_kbd_proxy", AsyncMock(return_value=_contract_response())),
        patch.object(kb.httpx, "AsyncClient", return_value=client_context),
    ):
        response = await kb.kbd_capabilities_proxy(_request())

    body = json.loads(response.body)
    by_id = {item["capability_id"]: item for item in body["capabilities"]}
    assert by_id["qkv_task"]["runtime_status"] == "available"
    assert by_id["qkv_task"]["verification_status"] == "runtime_discovered"
    assert by_id["qkv_task"]["runtime"]["usable"] is True
    assert by_id["qfk_system"]["runtime_status"] == "unknown"
    assert body["runtime_discovery"] == {"status": "available", "service": "agent-service"}


@pytest.mark.asyncio
async def test_kbd_capabilities_keeps_contract_when_agent_discovery_is_unreachable():
    """Agent 探测不可达时仍返回契约，并诚实保留 unknown。"""

    runtime_client = AsyncMock()
    runtime_client.get.side_effect = httpx.ConnectError(
        "agent unavailable",
        request=httpx.Request("GET", "http://agent-service/internal/capabilities"),
    )
    client_context = MagicMock()
    client_context.__aenter__ = AsyncMock(return_value=runtime_client)
    client_context.__aexit__ = AsyncMock(return_value=False)

    with (
        patch.object(kb, "_internal_auth_headers", return_value={"Authorization": "Bearer test"}),
        patch.object(kb, "_kbd_proxy", AsyncMock(return_value=_contract_response())),
        patch.object(kb.httpx, "AsyncClient", return_value=client_context),
    ):
        response = await kb.kbd_capabilities_proxy(_request())

    body = json.loads(response.body)
    assert all(item["runtime_status"] == "unknown" for item in body["capabilities"])
    assert all(item["verification_status"] == "contract_only" for item in body["capabilities"])
    assert body["runtime_discovery"] == {"status": "unavailable", "service": None}
