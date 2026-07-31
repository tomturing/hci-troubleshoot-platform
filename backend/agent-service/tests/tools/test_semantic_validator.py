"""
工具执行前语义校验测试。
"""

from app.tools.acli.semantic_validator import ToolSemanticValidator, build_container_command
from app.tools.base_tool import ToolDefinition


def _validate(tool_name: str, args: dict):
    return ToolSemanticValidator.validate(tool_name, args)


def _bash_tool_with_containers(containers: list[str]) -> ToolDefinition:
    return ToolDefinition(
        name="bash_exec",
        description="执行 Bash 命令",
        parameters={
            "type": "object",
            "properties": {
                "container": {"type": "string", "enum": containers},
                "command": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["container", "command", "reason"],
        },
        risk_level=1,
        policy="auto",
        category="acli",
    )


def test_bash_exec_requires_container():
    result = _validate("bash_exec", {"command": "ps aux", "reason": "检查进程"})
    assert not result.ok
    assert result.issues[0].code == "BASH_CONTAINER_REQUIRED"


def test_bash_exec_rejects_invalid_container():
    result = _validate("bash_exec", {"container": "bad", "command": "ps aux", "reason": "检查进程"})
    assert not result.ok
    assert result.issues[0].code == "BASH_CONTAINER_INVALID"


def test_bash_exec_rejects_container_prefix_in_command():
    result = _validate("bash_exec", {"container": "asv-con", "command": "docker exec x ps aux", "reason": "检查进程"})
    assert not result.ok
    assert result.issues[0].code == "BASH_CONTAINER_PREFIX_FORBIDDEN"


def test_bash_exec_rejects_acli_command():
    result = _validate("bash_exec", {"container": "asv-con", "command": "acli vm list", "reason": "检查 VM"})
    assert not result.ok
    assert result.issues[0].code == "BASH_ACLI_FORBIDDEN"


def test_bash_exec_accepts_valid_command():
    result = _validate("bash_exec", {"container": "asv-con", "command": "ps aux", "reason": "检查进程"})
    assert result.ok


def test_bash_exec_container_enum_comes_from_tool_definition():
    tool_def = _bash_tool_with_containers(["host", "vs-cp-manager", "custom-con"])

    accepted = ToolSemanticValidator.validate(
        "bash_exec",
        {"container": "custom-con", "command": "ps aux", "reason": "检查进程"},
        tool_def=tool_def,
    )
    rejected = ToolSemanticValidator.validate(
        "bash_exec",
        {"container": "asv-con", "command": "ps aux", "reason": "检查进程"},
        tool_def=tool_def,
    )

    assert accepted.ok
    assert not rejected.ok
    assert rejected.issues[0].code == "BASH_CONTAINER_INVALID"


def test_bash_exec_accepts_host_command():
    result = _validate("bash_exec", {"container": "host", "command": "ls -h", "reason": "检查物理机目录"})
    assert result.ok


def test_build_container_command_quotes_user_command():
    built = build_container_command("asv-con", "grep ERROR /sf/log/vtpdaemon.log | tail -50")
    assert built.startswith("HCI_CONTAINER=asv-con;")
    assert "HCI_RUNTIME=$(sh -lc" in built
    assert 'container_exec -n "$HCI_CONTAINER" -c "$HCI_USER_COMMAND" -d' in built
    assert 'docker exec "$HCI_CONTAINER"' in built
    assert "grep ERROR" in built


def test_build_host_command_does_not_wrap():
    assert build_container_command("host", "ls -h") == "ls -h"


def test_acli_exec_catalog_accepts_supported_command_with_global_option():
    result = _validate("acli_exec", {"command": "acli --formatter json vm list", "reason": "检查 VM"})
    assert result.ok


def test_acli_exec_catalog_treats_cluster_as_boolean_global_option():
    result = _validate("acli_exec", {"command": "acli --cluster --container asv-con system df /sf/log", "reason": "检查日志盘"})
    assert result.ok


def test_acli_exec_catalog_unknown_command_requires_confirmation_by_default():
    result = _validate("acli_exec", {"command": "acli storage disk list", "reason": "检查磁盘"})
    assert result.ok
    assert result.issues[0].code == "ACLI_COMMAND_NOT_IN_CATALOG_CONFIRM"
    assert result.issues[0].level == "warning"


def test_acli_exec_catalog_unknown_command_can_be_denied_in_production(monkeypatch):
    monkeypatch.setenv("ACLI_UNKNOWN_COMMAND_POLICY", "deny")
    result = _validate("acli_exec", {"command": "acli storage disk list", "reason": "检查磁盘"})
    assert not result.ok
    assert result.issues[0].code == "ACLI_COMMAND_NOT_IN_CATALOG"


def test_acli_exec_allows_help_for_exploration():
    result = _validate("acli_exec", {"command": "acli storage --help", "reason": "查看帮助"})
    assert result.ok
