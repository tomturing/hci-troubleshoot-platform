"""KBD 关键信号的只读领域边界。

KBD Signal 只描述事实采集、确定性判定或变量生产。处置/修复动作仍属于
KBD 正文的解决方案，不得进入 ``signals_json``。本模块刻意不实现通用 Shell
风险分析，只识别平台既有的明确写动作命令词表；LLM 错把“修复后执行的只读
验证”标成 solution 时，可由封闭只读命令表证明并纠正，不能误报为 write_signal。
"""

from __future__ import annotations

import re
from typing import Any

from jsonschema import ValidationError

WRITE_OPERATION_COMMANDS = frozenset(
    {
        "start",
        "stop",
        "shutdown",
        "restart",
        "suspend",
        "resume",
        "migrate",
        "clone",
        "snapshot",
        "reset",
        "reboot",
        "delete",
        "remove",
        "del",
        "rm",
        "format",
        "wipe",
        "destroy",
        "enable",
        "disable",
        "kill",
        "killall",
        "pkill",
        "up",
        "down",
        "set",
        "create",
        "add",
        "modify",
        "update",
    }
)
DESTRUCTIVE_OPERATION_COMMANDS = frozenset(
    {"delete", "remove", "del", "rm", "format", "wipe", "destroy"}
)
READ_ONLY_SYSTEM_COMMANDS = frozenset(
    {
        "cat",
        "df",
        "dmidecode",
        "du",
        "ethtool",
        "free",
        "iostat",
        "ll",
        "ls",
        "lsblk",
        "lsof",
        "lspci",
        "lsmod",
        "ps",
        "realethtool",
        "sensors",
        "smartctl",
        "stat",
    }
)
READ_ONLY_SUBCOMMAND_SUFFIXES = frozenset({"check", "get", "info", "list", "show", "status"})

_REMEDIATION_GUIDANCE = (
    "处置动作不属于 KBD 关键信号；请修正案例中的排查描述，"
    "或将该动作保留在 KBD 解决方案中"
)


def kbd_signal_read_only_violation(signal: Any) -> str | None:
    """返回 KBD Signal 的只读边界违规原因；合法时返回 ``None``。"""

    if not isinstance(signal, dict):
        return None

    write_command = signal_write_operation_command(signal)
    if write_command:
        return f"检测到写操作命令 {write_command}；{_REMEDIATION_GUIDANCE}"

    orchestrate = signal.get("orchestrate") or {}
    if (
        str(orchestrate.get("phase") or "diagnostic") == "solution"
        and not signal_explicitly_read_only_command(signal)
    ):
        return f"检测到处置阶段信号（orchestrate.phase=solution）；{_REMEDIATION_GUIDANCE}"
    return None


def signal_explicitly_read_only_command(signal: Any) -> bool:
    """封闭证明 Candidate 的实际执行命令是只读采集。"""

    if not isinstance(signal, dict):
        return False
    acquire = signal.get("acquire") or {}
    tool = str(acquire.get("tool") or "")
    if tool == "qfk_log":
        return True
    if not tool.startswith("qfk_"):
        return False
    command = str((acquire.get("args") or {}).get("command") or "").strip().lower()
    tokens = [token for token in re.split(r"[\s|/]+", command) if token]
    if not tokens:
        return False
    if tool == "qfk_system":
        return tokens[0] in READ_ONLY_SYSTEM_COMMANDS
    return tokens[-1] in READ_ONLY_SUBCOMMAND_SUFFIXES


def signal_write_operation_command(signal: Any) -> str | None:
    """返回既有明确写动作词表命中的命令；不推断命令参数语义。"""

    if not isinstance(signal, dict):
        return None
    acquire = signal.get("acquire") or {}
    tool = str(acquire.get("tool") or "")
    if not tool.startswith("qfk_"):
        return None
    command = str((acquire.get("args") or {}).get("command") or "").strip().lower()
    tokens = {token for token in re.split(r"[\s|/]+", command) if token}
    write_tokens = sorted(tokens & WRITE_OPERATION_COMMANDS)
    return write_tokens[0] if write_tokens else None


def signal_write_operation_risk(signal: Any) -> int:
    """保持既有风险等级：破坏性动作 block，其余明确写动作 require confirm。"""

    command = signal_write_operation_command(signal)
    return 3 if command in DESTRUCTIVE_OPERATION_COMMANDS else 2


def validate_kbd_read_only_signals_json(raw: Any) -> None:
    """校验整份 KBD ``signals_json`` 不含处置/修复 Signal。"""

    if not isinstance(raw, dict):
        return
    for index, signal in enumerate(raw.get("signals") or []):
        reason = kbd_signal_read_only_violation(signal)
        if reason:
            raise ValidationError(
                f"signals[{index}] {reason}",
                path=["signals", index],
            )
