"""
工具执行前语义校验测试。
"""

from app.tools.acli.semantic_validator import ToolSemanticValidator, build_container_command


def _validate(tool_name: str, args: dict):
    return ToolSemanticValidator.validate(tool_name, args)


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


def test_bash_exec_accepts_host_command():
    result = _validate("bash_exec", {"container": "host", "command": "ls -h", "reason": "检查物理机目录"})
    assert result.ok


def test_build_container_command_quotes_user_command():
    built = build_container_command("asv-con", "grep ERROR /sf/log/vtpdaemon.log | tail -50")
    assert built.startswith("HCI_CONTAINER=asv-con;")
    assert "HCI_RUNTIME=$(sh -lc" in built
    assert "container_exec -n \"$HCI_CONTAINER\" -c \"$HCI_USER_COMMAND\" -d" in built
    assert "docker exec \"$HCI_CONTAINER\"" in built
    assert "grep ERROR" in built


def test_build_host_command_does_not_wrap():
    assert build_container_command("host", "ls -h") == "ls -h"


def test_acli_exec_catalog_accepts_supported_command_with_global_option():
    result = _validate("acli_exec", {"command": "acli --formatter json vm list", "reason": "检查 VM"})
    assert result.ok


def test_acli_exec_catalog_rejects_unsupported_command():
    result = _validate("acli_exec", {"command": "acli storage disk list", "reason": "检查磁盘"})
    assert not result.ok
    assert result.issues[0].code == "ACLI_COMMAND_NOT_IN_CATALOG"


def test_acli_exec_allows_help_for_exploration():
    result = _validate("acli_exec", {"command": "acli storage --help", "reason": "查看帮助"})
    assert result.ok
