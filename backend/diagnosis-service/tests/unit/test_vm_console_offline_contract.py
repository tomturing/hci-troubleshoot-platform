"""qkv_vm_console 离线链路契约集成测试（§11.1 验收线 B 的进程内验证）。

覆盖：专用编译入口（绝不产出 command_template）、占位符 fail-closed、
制品执行项渲染（受控 Capture Intent）、策略门禁。真实 Go 采集器行为由
offline-collector Go 测试覆盖（固定 argv / TTY 唤醒 / nearblack 同源）。
"""

import pytest
from app.services.collector_artifact_service import CollectorArtifactService
from app.services.offline_acquisition_compiler import (
    CompiledVmConsoleCapture,
    compile_vm_console_capture_intent,
)


def test_compile_vm_console_capture_intent_freezes_targets():
    compiled = compile_vm_console_capture_intent(
        args={"host": "SVR_aCloud_668", "vm_id": "123", "timeout": 60},
    )

    assert isinstance(compiled, CompiledVmConsoleCapture)
    assert compiled.executor == "vm_console_capture"
    assert compiled.operation_version == "v1"
    assert compiled.capture_mode == "baseline_then_prompt_if_near_black"
    assert compiled.host_node_id == "SVR_aCloud_668"
    assert compiled.vm_id == "123"
    assert compiled.max_capture_bytes == 16 * 1024 * 1024
    # 审计快照证明经 Shared Resolution Runtime 校验（verified）。
    assert compiled.resolution_snapshot["status"] == "verified"
    assert compiled.resolution_snapshot["argv"] == []  # 绝不产出命令


def test_compile_vm_console_capture_intent_rejects_unresolved_placeholders():
    with pytest.raises(ValueError, match="host 未解析"):
        compile_vm_console_capture_intent(args={"host": "{{HOST}}", "vm_id": "123"})
    with pytest.raises(ValueError, match="vm_id 未解析"):
        compile_vm_console_capture_intent(args={"host": "SVR_aCloud_668", "vm_id": "{{VM_ID}}"})


def test_compile_vm_console_capture_intent_rejects_fuzzy_vm_names():
    with pytest.raises(ValueError, match="精确数值 VMID"):
        compile_vm_console_capture_intent(args={"host": "SVR_aCloud_668", "vm_id": "vm-web-01"})


def test_render_vm_console_capture_spec_is_gated_by_policy(monkeypatch):
    monkeypatch.delenv("VM_CONSOLE_CAPTURE_ENABLED", raising=False)

    class _Definition:
        collector_id = "kbd_qkv_vm_console_test"
        timeout_seconds = 60

    from app.errors import DiagnosisError

    with pytest.raises(DiagnosisError) as exc_info:
        CollectorArtifactService._render_vm_console_capture_spec(
            _Definition(), {"host": "SVR_aCloud_668", "vm_id": "123"}, {"type": "node", "id": "SVR_aCloud_668"}
        )
    assert exc_info.value.code == "VM_CONSOLE_DISABLED_BY_POLICY"


def test_render_vm_console_capture_spec_produces_fixed_intent(monkeypatch):
    monkeypatch.setenv("VM_CONSOLE_CAPTURE_ENABLED", "true")

    class _Definition:
        collector_id = "kbd_qkv_vm_console_test"
        timeout_seconds = 60

    execution_spec, rendered_command = CollectorArtifactService._render_vm_console_capture_spec(
        _Definition(), {"host": "SVR_aCloud_668", "vm_id": "123"}, {"type": "node", "id": "SVR_aCloud_668"}
    )

    assert execution_spec["executor"] == "vm_console_capture"
    assert execution_spec["operation_version"] == "v1"
    assert execution_spec["capture_mode"] == "baseline_then_prompt_if_near_black"
    assert execution_spec["host_node_id"] == "SVR_aCloud_668"
    assert execution_spec["vm_id"] == "123"
    assert execution_spec["max_capture_bytes"] == 16 * 1024 * 1024
    assert execution_spec["artifact_policy"] == "vm_console_v1"
    # rendered_command 仅是执行项标识，不是可执行命令模板。
    assert rendered_command == "vm_console_capture://SVR_aCloud_668/123"
    assert "vtpsh" not in rendered_command


def test_render_vm_console_capture_spec_requires_targets(monkeypatch):
    monkeypatch.setenv("VM_CONSOLE_CAPTURE_ENABLED", "true")

    class _Definition:
        collector_id = "kbd_qkv_vm_console_test"
        timeout_seconds = 60

    from app.errors import DiagnosisError

    with pytest.raises(DiagnosisError) as exc_info:
        CollectorArtifactService._render_vm_console_capture_spec(
            _Definition(), {"vm_id": "123"}, {"type": "variable", "id": "source_node"}
        )
    assert exc_info.value.code == "VM_CONSOLE_TARGET_MISSING"
