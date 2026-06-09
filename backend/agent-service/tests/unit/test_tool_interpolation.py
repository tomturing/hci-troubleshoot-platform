"""
Unit tests for TemplateInterpolator
"""

import shlex

import pytest
from app.tools.acli.executor import TemplateInterpolator


def test_successful_interpolation():
    # 测试常规正常插值
    template = "acli plugins vm_start vm_start --vm-id {vm_id}"
    args = {"vm_id": "vm-12345", "node_ip": "10.0.0.1"}
    command = TemplateInterpolator.interpolate(template, args)
    assert command == "acli plugins vm_start vm_start --vm-id vm-12345"


def test_shell_injection_protection():
    # 测试注入攻击逃逸保护
    template = "acli plugins vm_start vm_start --vm-id {vm_id}"
    args = {"vm_id": "vm-12345; rm -rf /"}
    command = TemplateInterpolator.interpolate(template, args)
    expected = f"acli plugins vm_start vm_start --vm-id {shlex.quote('vm-12345; rm -rf /')}"
    assert command == expected


def test_missing_parameter_raises_error():
    # 测试必填参数缺失报错
    template = "acli plugins vm_start vm_start --vm-id {vm_id} --disk-id {disk_id}"
    args = {"vm_id": "vm-12345"}
    with pytest.raises(ValueError) as excinfo:
        TemplateInterpolator.interpolate(template, args)
    assert "disk_id" in str(excinfo.value)
