"""qkv_vm_console 在线适配器链路单元测试（对齐设计文档 §11.4）。

覆盖：FrontendSignal 扩展、视觉观察解析分支、目标形态校验、视觉策略门禁、
执行层 fail-closed 开关。Bridge 固定操作本身由 terminal_bridge Go 测试覆盖。
"""

import json

import pytest
from app.tools.qkv.parser import parse_frontend_value
from app.tools.qkv.signal import FrontendQueryType, FrontendSignal
from app.tools.vm_console import inventory
from app.tools.vm_console.adapter import capture_enabled, run_vm_console_signal
from app.tools.vm_console.vision_extractor import (
    _parse_model_payload,
    derive_png_from_ppm,
    vision_allowed,
)
from pydantic import ValidationError

# ─── FrontendSignal / qmap ─────────────────────────────────────────────


def _vm_console_signal_v2(**overrides):
    args = {"host": "{{HOST}}", "vm_id": "{{VM_ID}}", "timeout": 60}
    args.update(overrides)
    return {
        "id": "s_vm_console",
        "acquire": {"tool": "qkv_vm_console", "args": args},
        "match": None,
        "orchestrate": {
            "requires": ["HOST", "VM_ID"],
            "produces": [
                {"name": "VM_CONSOLE_STATE", "path": "display_state"},
                {"name": "VM_CONSOLE_ARTIFACT_ID", "path": "artifact_id"},
            ],
        },
    }


def test_frontend_signal_maps_qkv_vm_console_tool():
    signal = FrontendSignal.from_dict(_vm_console_signal_v2())

    assert signal.query == FrontendQueryType.VM_CONSOLE
    assert signal.host == "{{HOST}}"
    assert signal.vm_id == "{{VM_ID}}"
    assert signal.timeout == 60
    assert len(signal.produces) == 2


def test_frontend_signal_vm_console_requires_targets():
    with pytest.raises(ValidationError):
        FrontendSignal.from_dict(_vm_console_signal_v2(host=""))
    with pytest.raises(ValidationError):
        FrontendSignal.from_dict(_vm_console_signal_v2(vm_id=""))


def test_frontend_signal_vm_console_timeout_bounded():
    with pytest.raises(ValidationError):
        FrontendSignal.from_dict(_vm_console_signal_v2(timeout=300))


def test_signal_to_qkv_never_builds_free_text_signal_for_vm_console():
    """_signal_to_qkv 必须显式拒绝 vm_console，防止落入 qkv_exec 自由文本路径。"""

    from app.adapters.agents.htp.kbd_differential import KBDDiagnostic

    diag = KBDDiagnostic(ai_registry=None, tool_executor=None)
    assert diag._signal_to_qkv(_vm_console_signal_v2(), {}) is None


# ─── parse_frontend_value VM_CONSOLE 分支 ──────────────────────────────


def test_parse_vm_console_observation_with_produces():
    observation = {
        "observation_status": "observed",
        "display_state": "kernel_panic",
        "summary": "控制台显示 Kernel panic - not syncing",
        "confidence": 0.91,
        "artifact_id": "artifact-001",
    }
    values = parse_frontend_value(
        FrontendQueryType.VM_CONSOLE,
        json.dumps(observation),
        produces=[
            {"name": "VM_CONSOLE_STATE", "path": "display_state"},
            {"name": "VM_CONSOLE_CONFIDENCE", "path": "confidence"},
            {"name": "VM_CONSOLE_ARTIFACT_ID", "path": "artifact_id"},
        ],
    )

    assert values[0]["vm_console_state"] == "kernel_panic"
    assert values[0]["vm_console_confidence"] == 0.91
    assert values[0]["vm_console_artifact_id"] == "artifact-001"


def test_parse_vm_console_observation_hardcoded_fallback():
    observation = {"display_state": "black_screen", "summary": "黑屏", "confidence": 0.8, "artifact_id": "a"}
    values = parse_frontend_value(FrontendQueryType.VM_CONSOLE, json.dumps(observation), produces=None)

    assert values[0]["vm_console_state"] == "black_screen"


def test_parse_vm_console_invalid_json_returns_empty():
    assert parse_frontend_value(FrontendQueryType.VM_CONSOLE, "not-json", produces=None) == []


# ─── 目标形态校验 ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "host,vm_id,ok",
    [
        ("SVR_aCloud_668", "123", True),
        ("node-01.cluster", "9", True),
        ("{{HOST}}", "123", False),  # 占位符未解析
        ("SVR_aCloud_668", "{{VM_ID}}", False),
        ("node;reboot", "123", False),
        ("SVR_aCloud_668", "vm-web-01", False),  # 模糊 VM 名称
        ("", "123", False),
    ],
)
def test_validate_target_shape(host, vm_id, ok):
    result, _reason = inventory.validate_target_shape(host, vm_id)
    assert result is ok


def test_verify_vm_target_sim_inventory(monkeypatch):
    monkeypatch.setenv("VM_CONSOLE_SIM_INVENTORY", "123=SVR_SIM_01,456=SVR_SIM_02")

    import asyncio

    matched = asyncio.run(inventory.verify_vm_target("SVR_SIM_01", "123"))
    assert matched.verified and matched.source == "sim_inventory"

    mismatched = asyncio.run(inventory.verify_vm_target("SVR_SIM_02", "123"))
    assert not mismatched.verified and "归属不匹配" in mismatched.reason


def test_verify_vm_target_fails_closed_without_inventory(monkeypatch):
    monkeypatch.delenv("VM_CONSOLE_SIM_INVENTORY", raising=False)

    import asyncio

    result = asyncio.run(inventory.verify_vm_target("SVR_aCloud_668", "123", scp_client=None))
    assert not result.verified
    assert result.source == "unresolved"


# ─── 视觉提取 ───────────────────────────────────────────────────────────


def _ppm_bytes(pixel=(10, 200, 30), width=8, height=8) -> bytes:
    return f"P6\n{width} {height}\n255\n".encode() + bytes(pixel) * (width * height)


def test_derive_png_from_ppm():
    png, width, height = derive_png_from_ppm(_ppm_bytes())
    assert png.startswith(b"\x89PNG")
    assert (width, height) == (8, 8)


def test_parse_model_payload_tolerates_fences():
    payload = _parse_model_payload('```json\n{"display_state": "bsod", "confidence": 0.9}\n```')
    assert payload is not None and payload["display_state"] == "bsod"
    assert _parse_model_payload("无法解析的文本") is None


def test_vision_policy_gate_defaults_closed(monkeypatch):
    monkeypatch.delenv("VM_CONSOLE_VISION_ALLOWED", raising=False)
    assert vision_allowed() is False

    import asyncio

    from app.tools.vm_console.vision_extractor import extract_observation

    observation = asyncio.run(extract_observation(_ppm_bytes(), artifact_id="artifact-x"))
    assert observation.observation_status == "unavailable"
    assert observation.display_state == "unknown"
    assert "VISION_UNAVAILABLE_BY_POLICY" in observation.summary


# ─── 执行层策略开关 ─────────────────────────────────────────────────────


def test_capture_disabled_by_default(monkeypatch):
    monkeypatch.delenv("VM_CONSOLE_CAPTURE_ENABLED", raising=False)
    assert capture_enabled() is False


def test_run_vm_console_signal_fails_closed_when_disabled(monkeypatch):
    monkeypatch.delenv("VM_CONSOLE_CAPTURE_ENABLED", raising=False)

    import asyncio

    result = asyncio.run(
        run_vm_console_signal(
            _vm_console_signal_v2(host="SVR_aCloud_668", vm_id="123"),
            {},
            conversation_id="conv",
            case_id="Q1",
            db_session_factory=None,
        )
    )
    assert not result.success
    assert result.error_code == "VM_CONSOLE_DISABLED_BY_POLICY"
