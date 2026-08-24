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
    monkeypatch.delenv("VM_CONSOLE_CAPTURE_ENABLED", raising=False)
    monkeypatch.delenv("EFFECT_VERIFICATION_ENABLED", raising=False)

    document = capability_routes.runtime_capability_document()
    capabilities = {item["capability_id"]: item for item in document["capabilities"]}

    assert document["count"] == 13
    assert set(capabilities) == {
        "qkv_alert",
        "qkv_task",
        "qkv_dialog",
        "qkv_vm_console",
        "qkv_effect",
        *(f"qfk_{namespace}" for namespace in HandlerRegistry.supported_namespaces()),
    }
    assert all(item["implemented"] for item in capabilities.values())
    assert all(item["validator_ready"] for item in capabilities.values())
    assert all(item["runtime_status"] == "degraded" for item in capabilities.values())
    assert all(not item["usable"] for item in capabilities.values())
    # 条件型生产者的降级原因是各自策略门禁，而非执行桥缺失。
    vm_console = capabilities["qkv_vm_console"]
    assert vm_console["conditional_producer"] is True
    assert vm_console["controlled_interaction"] is True
    assert "VM_CONSOLE_CAPTURE_ENABLED" in vm_console["reason"]
    effect = capabilities["qkv_effect"]
    assert effect["conditional_producer"] is True
    # 效果验证严格只读：条件型生产者但无受控交互。
    assert effect["controlled_interaction"] is False
    assert "EFFECT_VERIFICATION_ENABLED" in effect["reason"]
    assert all(
        "Executor 尚未注入" in item["reason"]
        for capability_id, item in capabilities.items()
        if capability_id not in {"qkv_vm_console", "qkv_effect"}
    )


def test_runtime_discovery_reports_available_only_after_executor_is_injected(monkeypatch):
    """当前 Pod 已注入执行桥后，已注册 Capability 才能标记为 available。"""

    monkeypatch.setattr(executor_module, "_executor", object())
    monkeypatch.delenv("VM_CONSOLE_CAPTURE_ENABLED", raising=False)
    monkeypatch.delenv("EFFECT_VERIFICATION_ENABLED", raising=False)

    capabilities = capability_routes.runtime_capability_document()["capabilities"]

    # 条件型生产者在各自执行层开关关闭时保持 degraded；其余能力 available。
    direct = [item for item in capabilities if not item.get("conditional_producer")]
    assert all(item["runtime_status"] == "available" for item in direct)
    assert all(item["executor_ready"] and item["usable"] for item in direct)
    assert all(item["reason"] is None for item in direct)
    conditional = [item for item in capabilities if item.get("conditional_producer")]
    assert len(conditional) == 2
    assert all(item["runtime_status"] == "degraded" and not item["usable"] for item in conditional)


def test_runtime_discovery_marks_vm_console_available_when_policy_enabled(monkeypatch):
    """执行桥注入且策略开关开启后，条件型视觉生产者才可标记为 available。"""

    monkeypatch.setattr(executor_module, "_executor", object())
    monkeypatch.setenv("VM_CONSOLE_CAPTURE_ENABLED", "true")

    capabilities = capability_routes.runtime_capability_document()["capabilities"]
    vm_console = next(item for item in capabilities if item["capability_id"] == "qkv_vm_console")

    assert vm_console["runtime_status"] == "available"
    assert vm_console["usable"] is True
    assert vm_console["reason"] is None


def test_runtime_discovery_marks_effect_available_when_policy_enabled(monkeypatch):
    """执行桥注入且策略开关开启后，条件型效果验证生产者才可标记为 available。"""

    monkeypatch.setattr(executor_module, "_executor", object())
    monkeypatch.setenv("EFFECT_VERIFICATION_ENABLED", "true")

    capabilities = capability_routes.runtime_capability_document()["capabilities"]
    effect = next(item for item in capabilities if item["capability_id"] == "qkv_effect")

    assert effect["runtime_status"] == "available"
    assert effect["usable"] is True
    assert effect["reason"] is None


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
