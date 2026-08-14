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


def test_validate_mustache_template_and_nested_parameter_path():
    """Tool 模板校验必须识别 Mustache 条件段和嵌套字段。"""

    result = validate_tool_payload(
        {
            "tool_name": "qfk_log",
            "usage_template": ("acli log get -k {{keyword}} {{#if target.resource}}-f {{target.resource}}{{/if}}"),
            "parameters_schema": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string"},
                    "target": {
                        "type": "object",
                        "properties": {"resource": {"type": "string"}},
                    },
                },
            },
        }
    )

    assert result == {"status": "ok", "validation_issues": []}


def test_validate_mustache_template_rejects_undeclared_nested_path():
    """Mustache 嵌套占位符不得绕过参数模式校验。"""

    result = validate_tool_payload(
        {
            "tool_name": "qfk_log",
            "usage_template": "acli log get {{#if target.path}}-p {{target.path}}{{/if}}",
            "parameters_schema": {
                "type": "object",
                "properties": {"target": {"type": "object", "properties": {}}},
            },
        }
    )

    assert result["status"] == "error"
    assert {issue["code"] for issue in result["validation_issues"]} == {"USAGE_TEMPLATE_PLACEHOLDER_MISSING"}


def test_qkv_qfk_tool_allows_empty_display_template_but_requires_read_only_risk():
    """QKV/QFK 命令由 Resolver 生成；usage_template 仅作展示，风险门禁仍生效。"""

    result = validate_tool_payload(
        {
            "tool_name": "qfk_system",
            "category": "qfk",
            "usage_template": "",
            "parameters_schema": {"type": "object", "properties": {}},
            "risk_level": 3,
        }
    )

    assert {issue["code"] for issue in result["validation_issues"]} == {
        "SIGNAL_TOOL_MUST_BE_READ_ONLY",
    }


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


def test_validate_tool_name_with_dot_rejected():
    result = validate_tool_payload(
        {
            "tool_name": "qfk.hardware",
            "parameters_schema": {"type": "object", "properties": {}, "required": []},
        }
    )

    assert result["status"] == "error"
    codes = {issue["code"] for issue in result["validation_issues"]}
    assert "TOOL_NAME_INVALID_FORMAT" in codes


def test_validate_tool_name_uppercase_rejected():
    result = validate_tool_payload(
        {
            "tool_name": "AcliExec",
            "parameters_schema": {"type": "object", "properties": {}, "required": []},
        }
    )

    assert result["status"] == "error"
    codes = {issue["code"] for issue in result["validation_issues"]}
    assert "TOOL_NAME_INVALID_FORMAT" in codes


def test_validate_tool_name_valid_snake_case_ok():
    result = validate_tool_payload(
        {
            "tool_name": "qfk_hardware",
            "parameters_schema": {"type": "object", "properties": {}, "required": []},
        }
    )

    assert result["status"] == "ok"
