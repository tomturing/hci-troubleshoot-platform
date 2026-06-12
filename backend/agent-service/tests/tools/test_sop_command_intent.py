"""
SOP Markdown 命令归一化测试。
"""

from app.tools.sop.command_intent import normalize_sop_command


def test_normalize_acli_command_to_acli_exec():
    intent = normalize_sop_command("acli storage asan disk list", reason="检查磁盘")

    assert intent["tool_name"] == "acli_exec"
    assert intent["args"]["command"] == "acli storage asan disk list"
    assert intent["args"]["reason"] == "检查磁盘"
    assert intent["parse_status"] == "ok"


def test_normalize_container_exec_to_bash_exec():
    intent = normalize_sop_command('container_exec -n vs-cp-manager -c "smartctl -a /dev/sda"')

    assert intent["tool_name"] == "bash_exec"
    assert intent["args"]["container"] == "vs-cp-manager"
    assert intent["args"]["command"] == "smartctl -a /dev/sda"
    assert intent["parse_status"] == "ok"


def test_normalize_host_exec_to_bash_exec_host():
    intent = normalize_sop_command('host_exec -c "ls -h"')

    assert intent["tool_name"] == "bash_exec"
    assert intent["args"]["container"] == "host"
    assert intent["args"]["command"] == "ls -h"
    assert intent["parse_status"] == "ok"


def test_normalize_naked_command_defaults_to_host():
    intent = normalize_sop_command("ls -h")

    assert intent["tool_name"] == "bash_exec"
    assert intent["args"]["container"] == "host"
    assert intent["args"]["command"] == "ls -h"
    assert intent["parse_status"] == "ok"


def test_malformed_container_exec_returns_error_intent():
    intent = normalize_sop_command("container_exec -n vs-cp-manager")

    assert intent["tool_name"] == "bash_exec"
    assert intent["parse_status"] == "error"
