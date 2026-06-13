"""
工具定义契约校验测试。
"""

import pytest
from app.routes.tool_definition import _raise_if_invalid_tool_payload, validate_tool_payload
from fastapi import HTTPException


def test_validate_bash_exec_requires_container():
    result = validate_tool_payload(
        {
            "tool_name": "bash_exec",
            "parameters_schema": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["command", "reason"],
            },
        }
    )

    assert result["status"] == "error"
    codes = {issue["code"] for issue in result["validation_issues"]}
    assert "BASH_CONTAINER_MISSING" in codes
    assert "BASH_CONTAINER_REQUIRED" in codes


def test_validate_usage_template_placeholder():
    result = validate_tool_payload(
        {
            "tool_name": "acli_plugin_test",
            "usage_template": "acli plugins x --vm {vm_id}",
            "parameters_schema": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        }
    )

    assert result["status"] == "error"
    assert result["validation_issues"][0]["code"] == "USAGE_TEMPLATE_PLACEHOLDER_MISSING"


def test_validate_valid_bash_exec():
    result = validate_tool_payload(
        {
            "tool_name": "bash_exec",
            "parameters_schema": {
                "type": "object",
                "properties": {
                    "container": {
                        "type": "string",
                        "enum": ["host", "asv-con", "vn-con", "vn-agent", "vs-cp-manager"],
                    },
                    "command": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["container", "command", "reason"],
            },
        }
    )

    assert result == {"status": "ok", "validation_issues": []}


def test_validate_bash_exec_container_enum_is_data_driven():
    result = validate_tool_payload(
        {
            "tool_name": "bash_exec",
            "parameters_schema": {
                "type": "object",
                "properties": {
                    "container": {
                        "type": "string",
                        "enum": ["host", "custom-con"],
                    },
                    "command": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["container", "command", "reason"],
            },
        }
    )

    assert result == {"status": "ok", "validation_issues": []}


def test_validate_bash_exec_container_enum_requires_host_boundary():
    result = validate_tool_payload(
        {
            "tool_name": "bash_exec",
            "parameters_schema": {
                "type": "object",
                "properties": {
                    "container": {
                        "type": "string",
                        "enum": ["custom-con"],
                    },
                    "command": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["container", "command", "reason"],
            },
        }
    )

    assert result["status"] == "error"
    assert result["validation_issues"][0]["code"] == "BASH_CONTAINER_HOST_MISSING"


def test_save_guard_rejects_invalid_tool_payload():
    with pytest.raises(HTTPException) as exc_info:
        _raise_if_invalid_tool_payload(
            {
                "tool_name": "bash_exec",
                "parameters_schema": {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    "required": ["command", "reason"],
                },
            }
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail["status"] == "error"
