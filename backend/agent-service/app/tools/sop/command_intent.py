"""
SOP Markdown 命令归一化。

SOP 正文面向人编写，允许保留现场习惯命令。这里把命令字符串转换为
ReAct 更容易稳定调用的结构化工具意图，避免要求 SOP 作者直接编写 JSON tool_call。
"""

from __future__ import annotations

import shlex
from typing import Any

ALLOWED_SOP_BASH_CONTAINERS = {"host", "asv-con", "vn-con", "vn-agent", "vs-cp-manager"}


def normalize_sop_command(command: str, *, reason: str | None = None) -> dict[str, Any] | None:
    """把 SOP 命令字符串归一化为结构化工具意图。

    识别规则：
    - acli 开头：acli_exec
    - container_exec -n <container> -c <cmd>：bash_exec(container=<container>)
    - host_exec -c <cmd>：bash_exec(container=host)
    - 其他命令：bash_exec(container=host)
    """
    stripped = str(command or "").strip()
    if not stripped:
        return None

    tokens = _split_command(stripped)
    if not tokens:
        return _bash_intent(container="host", command=stripped, source_command=stripped, reason=reason)

    if tokens[0] == "acli":
        return {
            "tool_name": "acli_exec",
            "args": {
                "command": stripped,
                "reason": reason or "执行 SOP 中声明的 aCLI 检查命令",
            },
            "source_command": stripped,
            "parse_status": "ok",
        }

    if tokens[0] == "container_exec":
        parsed = _parse_container_exec(tokens)
        if parsed:
            container, inner_command = parsed
            return _bash_intent(container=container, command=inner_command, source_command=stripped, reason=reason)
        return {
            "tool_name": "bash_exec",
            "args": {
                "container": "",
                "command": stripped,
                "reason": reason or "SOP container_exec 命令解析失败，需要人工修正",
            },
            "source_command": stripped,
            "parse_status": "error",
            "error": "container_exec 命令必须包含 -n <container> 和 -c <command>",
        }

    if tokens[0] == "host_exec":
        inner_command = _parse_host_exec(tokens)
        if inner_command:
            return _bash_intent(container="host", command=inner_command, source_command=stripped, reason=reason)
        return {
            "tool_name": "bash_exec",
            "args": {
                "container": "host",
                "command": stripped,
                "reason": reason or "SOP host_exec 命令解析失败，需要人工修正",
            },
            "source_command": stripped,
            "parse_status": "error",
            "error": "host_exec 命令必须包含 -c <command>",
        }

    return _bash_intent(container="host", command=stripped, source_command=stripped, reason=reason)


def normalize_sop_commands(commands: list[str], *, reason: str | None = None) -> list[dict[str, Any]]:
    """批量归一化 SOP 命令。"""
    intents: list[dict[str, Any]] = []
    for command in commands:
        intent = normalize_sop_command(command, reason=reason)
        if intent:
            intents.append(intent)
    return intents


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

    if container not in ALLOWED_SOP_BASH_CONTAINERS - {"host"} or not command:
        return None
    return container, command


def _parse_host_exec(tokens: list[str]) -> str | None:
    idx = 1
    while idx < len(tokens):
        token = tokens[idx]
        if token in ("-c", "--cmd") and idx + 1 < len(tokens):
            return tokens[idx + 1]
        idx += 1
    return None


def _bash_intent(*, container: str, command: str, source_command: str, reason: str | None) -> dict[str, Any]:
    return {
        "tool_name": "bash_exec",
        "args": {
            "container": container,
            "command": command,
            "reason": reason or "执行 SOP 中声明的 Bash 检查命令",
        },
        "source_command": source_command,
        "parse_status": "ok",
    }
