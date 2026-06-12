"""
SOP Markdown 命令归一化工具。

kb-service 只做发布阶段静态解析与校验，不执行命令。agent-service 有同名逻辑用于
运行时返回 tool_calls；两边保持规则一致，避免微服务运行时互相 import。
"""

from __future__ import annotations

import shlex
from typing import Any

ALLOWED_SOP_BASH_CONTAINERS = {"host", "asv-con", "vn-con", "vn-agent", "vs-cp-manager"}


def normalize_sop_command(command: str) -> dict[str, Any] | None:
    """把 SOP 命令归一化为工具意图。"""
    stripped = str(command or "").strip()
    if not stripped:
        return None

    tokens = _split_command(stripped)
    if not tokens:
        return _bash_intent("host", stripped, stripped)

    if tokens[0] == "acli":
        return {"tool_name": "acli_exec", "args": {"command": stripped}, "source_command": stripped, "parse_status": "ok"}

    if tokens[0] == "container_exec":
        parsed = _parse_container_exec(tokens)
        if parsed:
            container, inner_command = parsed
            return _bash_intent(container, inner_command, stripped)
        return {
            "tool_name": "bash_exec",
            "args": {"container": "", "command": stripped},
            "source_command": stripped,
            "parse_status": "error",
            "error": "container_exec 命令必须包含 -n <container> 和 -c <command>",
        }

    if tokens[0] == "host_exec":
        inner_command = _parse_host_exec(tokens)
        if inner_command:
            return _bash_intent("host", inner_command, stripped)
        return {
            "tool_name": "bash_exec",
            "args": {"container": "host", "command": stripped},
            "source_command": stripped,
            "parse_status": "error",
            "error": "host_exec 命令必须包含 -c <command>",
        }

    return _bash_intent("host", stripped, stripped)


def _split_command(command: str) -> list[str]:
    try:
        return shlex.split(command)
    except ValueError:
        return []


def _parse_container_exec(tokens: list[str]) -> tuple[str, str] | None:
    container = ""
    command = ""
    idx = 1
    while idx < len(tokens):
        token = tokens[idx]
        if token in ("-n", "--name") and idx + 1 < len(tokens):
            container = tokens[idx + 1]
            idx += 2
            continue
        if token in ("-c", "--cmd") and idx + 1 < len(tokens):
            command = tokens[idx + 1]
            idx += 2
            continue
        idx += 1

    if container not in (ALLOWED_SOP_BASH_CONTAINERS - {"host"}) or not command:
        return None
    return container, command


def _parse_host_exec(tokens: list[str]) -> str | None:
    idx = 1
    while idx < len(tokens):
        if tokens[idx] in ("-c", "--cmd") and idx + 1 < len(tokens):
            return tokens[idx + 1]
        idx += 1
    return None


def _bash_intent(container: str, command: str, source_command: str) -> dict[str, Any]:
    return {
        "tool_name": "bash_exec",
        "args": {"container": container, "command": command},
        "source_command": source_command,
        "parse_status": "ok",
    }
