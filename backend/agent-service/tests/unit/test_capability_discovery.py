"""Agent Capability 运行时发现单元测试。"""

import pytest
from app.routes import capabilities as capability_routes
from app.tools.acli import executor as executor_module
from app.tools.qfk.handlers import HandlerRegistry
from fastapi import HTTPException
from starlette.requests import Request


def _request(token: str | None = None) -> Request:
    headers = [] if token is None else [(b"authorization", f"Bearer {token}".encode())]
    return Request({"type": "http", "method": "GET", "path": "/internal/capabilities", "headers": headers})


def test_runtime_discovery_reports_all_registered_handlers_as_degraded_without_executor(monkeypatch):
    """参数契约与 Handler 已部署，但执行桥未注入时不得宣称可执行。"""

    monkeypatch.setattr(executor_module, "_executor", None)

    document = capability_routes.runtime_capability_document()
    capabilities = {item["capability_id"]: item for item in document["capabilities"]}

    assert document["count"] == 11
    assert set(capabilities) == {
        "qkv_alert",
        "qkv_task",
        "qkv_dialog",
        *(f"qfk_{namespace}" for namespace in HandlerRegistry.supported_namespaces()),
    }
    assert all(item["implemented"] for item in capabilities.values())
    assert all(item["validator_ready"] for item in capabilities.values())
    assert all(item["runtime_status"] == "degraded" for item in capabilities.values())
    assert all(not item["usable"] for item in capabilities.values())
    assert all("Executor 尚未注入" in item["reason"] for item in capabilities.values())


def test_runtime_discovery_reports_available_only_after_executor_is_injected(monkeypatch):
    """当前 Pod 已注入执行桥后，已注册 Capability 才能标记为 available。"""

    monkeypatch.setattr(executor_module, "_executor", object())

    capabilities = capability_routes.runtime_capability_document()["capabilities"]

    assert all(item["runtime_status"] == "available" for item in capabilities)
    assert all(item["executor_ready"] and item["usable"] for item in capabilities)
    assert all(item["reason"] is None for item in capabilities)


@pytest.mark.asyncio
async def test_runtime_discovery_rejects_missing_or_invalid_internal_token():
    """运行时发现只允许服务间内部 Token 调用。"""

    for request in (_request(), _request("invalid-token")):
        with pytest.raises(HTTPException) as exc_info:
            await capability_routes.get_runtime_capabilities(request)
        assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_runtime_discovery_accepts_configured_internal_token():
    """正确内部 Token 可读取当前进程的确定性运行时快照。"""

    document = await capability_routes.get_runtime_capabilities(
        _request(capability_routes.settings.INTERNAL_API_TOKEN),
    )

    assert document["service"] == capability_routes.settings.SERVICE_NAME
    assert document["schema_version"] == 1
