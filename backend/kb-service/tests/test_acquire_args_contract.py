"""QKV/QFK acquire 参数类型边界回归。"""

import pytest
from shared.schemas.acquirer_args import (
    QKV_KEYWORD_TYPE_ERROR_CODE,
    validate_acquire_args,
)


def test_qkv_keyword_is_a_single_string():
    ok, error = validate_acquire_args(
        "qkv_task",
        {"keyword": "迁移虚拟机", "is_failed": True, "limit": 1},
    )

    assert ok is True
    assert error is None


def test_qkv_keyword_array_is_rejected_with_actionable_code():
    ok, error = validate_acquire_args(
        "qkv_task",
        {"keyword": ["迁移虚拟机"], "is_failed": True, "limit": 1},
    )

    assert ok is False
    assert error is not None
    assert QKV_KEYWORD_TYPE_ERROR_CODE in error
    assert "match.pattern" in error
    assert "extract.rows.include/exclude" in error


def test_qkv_keyword_non_string_types_use_the_same_contract_code():
    ok, error = validate_acquire_args("qkv_alert", {"keyword": 123})

    assert ok is False
    assert error is not None
    assert error.startswith(f"{QKV_KEYWORD_TYPE_ERROR_CODE}:")


# ─── qkv_vm_console 条件型视觉生产者参数契约 ─────────────────────────────


def test_vm_console_accepts_placeholder_targets():
    ok, error = validate_acquire_args(
        "qkv_vm_console",
        {"host": "{{HOST}}", "vm_id": "{{VM_ID}}", "capture_mode": "baseline_then_optional_wake", "timeout": 60},
    )

    assert ok is True
    assert error is None


def test_vm_console_requires_host_and_vm_id():
    ok, error = validate_acquire_args("qkv_vm_console", {"host": "{{HOST}}"})

    assert ok is False
    assert "vm_id" in str(error)


@pytest.mark.parametrize(
    "extra_field",
    ["command", "monitor_command", "path", "key", "sleep", "shell", "url", "filename"],
)
def test_vm_console_rejects_free_form_execution_fields(extra_field):
    ok, error = validate_acquire_args(
        "qkv_vm_console",
        {"host": "{{HOST}}", "vm_id": "{{VM_ID}}", extra_field: "anything"},
    )

    assert ok is False
    assert extra_field in str(error)
    assert "未注册字段" in str(error)


@pytest.mark.parametrize(
    "host",
    ["", "  ", "node;rm -rf /", "$(reboot)", "a b", "node|nc"],
)
def test_vm_console_rejects_unsafe_host_literals(host):
    ok, error = validate_acquire_args("qkv_vm_console", {"host": host, "vm_id": "{{VM_ID}}"})

    assert ok is False
    assert "host" in str(error)


@pytest.mark.parametrize(
    "vm_id",
    ["", "vm-web-01", "{{VM}}", "12;34", "123abc"],
)
def test_vm_console_rejects_fuzzy_or_injected_vm_ids(vm_id):
    ok, error = validate_acquire_args("qkv_vm_console", {"host": "{{HOST}}", "vm_id": vm_id})

    assert ok is False
    assert "vm_id" in str(error)


def test_vm_console_allows_verified_numeric_vm_id_literal():
    ok, error = validate_acquire_args("qkv_vm_console", {"host": "SVR_aCloud_668", "vm_id": "123"})

    assert ok is True
    assert error is None


def test_vm_console_timeout_is_bounded_to_60_seconds():
    ok_timeout, _ = validate_acquire_args(
        "qkv_vm_console", {"host": "{{HOST}}", "vm_id": "{{VM_ID}}", "timeout": 60}
    )
    assert ok_timeout is True

    for value in (0, 61, 300):
        ok, error = validate_acquire_args(
            "qkv_vm_console", {"host": "{{HOST}}", "vm_id": "{{VM_ID}}", "timeout": value}
        )
        assert ok is False
        assert "1-60" in str(error)


def test_vm_console_capture_mode_is_fixed():
    ok, error = validate_acquire_args(
        "qkv_vm_console",
        {"host": "{{HOST}}", "vm_id": "{{VM_ID}}", "capture_mode": "send_any_key"},
    )

    assert ok is False
    assert "baseline_then_optional_wake" in str(error)
