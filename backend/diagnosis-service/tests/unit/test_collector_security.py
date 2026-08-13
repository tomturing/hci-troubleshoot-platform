"""Collector 命令安全与参数契约测试。"""

import pytest
from app.domain.collector_security import (
    render_collector_command,
    validate_collector_contract,
    validate_hci_api_contract,
    validate_manual_guide,
)
from app.errors import DiagnosisError


def parameter_schema() -> dict:
    """构造禁止额外字段的参数 Schema。"""

    return {
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
        },
        "required": ["limit"],
        "additionalProperties": False,
    }


def test_render_quotes_user_value_as_single_argv_token():
    """用户参数包含 Shell 元字符时只能作为单个 argv 参数。"""

    argv, rendered = render_collector_command(
        "/opt/vendor/raidcli show --target {target_id} --limit {limit}",
        parameter_schema(),
        {"target_id": "node-1; reboot", "limit": 10},
    )

    assert argv[-3:] == ["node-1; reboot", "--limit", "10"]
    assert "'node-1; reboot'" in rendered


@pytest.mark.parametrize(
    "template",
    [
        "raidcli show | cat",
        "raidcli show > /tmp/x",
        "raidcli $(whoami)",
        "bash -c reboot",
        "sudo raidcli show",
    ],
)
def test_unsafe_shell_composition_is_rejected(template):
    """管道、重定向、命令替换和通用 Shell 执行器被拒绝。"""

    with pytest.raises(DiagnosisError):
        validate_collector_contract(template, parameter_schema())


def test_placeholder_must_be_a_complete_token():
    """占位符不能和固定文本拼接，避免逃逸参数边界。"""

    with pytest.raises(DiagnosisError) as exc_info:
        validate_collector_contract("raidcli --target=node-{target_id}", parameter_schema())

    assert exc_info.value.code == "UNSAFE_COLLECTOR_PLACEHOLDER"


def test_parameter_schema_must_explicitly_forbid_extra_properties():
    """参数 Schema 必须显式禁止额外字段。"""

    schema = parameter_schema()
    del schema["additionalProperties"]

    with pytest.raises(DiagnosisError) as exc_info:
        validate_collector_contract("raidcli show --limit {limit}", schema)

    assert exc_info.value.code == "INVALID_PARAMETER_SCHEMA"


def test_json_schema_validation_rejects_out_of_range_value():
    """运行时参数必须通过 JSON Schema 范围校验。"""

    with pytest.raises(DiagnosisError) as exc_info:
        render_collector_command("raidcli show --limit {limit}", parameter_schema(), {"limit": 101})

    assert exc_info.value.code == "COLLECTOR_PARAMETER_VALIDATION_FAILED"


@pytest.mark.parametrize("template", ["/bin/rm -f /tmp/customer-data", "systemctl restart hci"])
def test_mutating_or_unlisted_commands_are_rejected(template):
    """read_only 标签不能让破坏性命令通过技术门禁。"""

    with pytest.raises(DiagnosisError):
        validate_collector_contract(template, {"type": "object", "properties": {}, "additionalProperties": False})


@pytest.mark.parametrize(
    "template",
    [
        "ip link add dummy0 type dummy",
        "ethtool -s eth0 speed 1000",
        "smartctl -t long /dev/sda",
        "nvme format /dev/nvme0",
        "journalctl --vacuum-time=1d",
    ],
)
def test_tool_specific_mutating_forms_are_rejected(template):
    """只读可执行程序的写子命令和写选项也必须拒绝。"""

    with pytest.raises(DiagnosisError) as exc_info:
        validate_collector_contract(template, {"type": "object", "properties": {}, "additionalProperties": False})

    assert exc_info.value.code == "MUTATING_COLLECTOR_COMMAND"


def test_rendered_dynamic_subcommand_is_checked_again():
    """模板占位符通过后，实际渲染出的写操作仍必须被拦截。"""

    schema = {
        "type": "object",
        "properties": {"command": {"type": "string"}},
        "required": ["command"],
        "additionalProperties": False,
    }

    with pytest.raises(DiagnosisError) as exc_info:
        render_collector_command("acli system {command}", schema, {"command": "restart"})

    assert exc_info.value.code == "MUTATING_COLLECTOR_COMMAND"


def test_hci_api_contract_only_accepts_fixed_relative_get():
    """HCI API Collector 只允许固定相对 GET 请求。"""

    empty_schema = {"type": "object", "properties": {}, "additionalProperties": False}
    validate_hci_api_contract("GET /api/v1/tasks?status=failed", empty_schema)
    validate_manual_guide("请导出支持信息并放入 attachments/support.zip", empty_schema)

    for request in ("POST /api/v1/tasks", "GET https://example.com/api", "GET /api/../secret"):
        with pytest.raises(DiagnosisError):
            validate_hci_api_contract(request, empty_schema)
