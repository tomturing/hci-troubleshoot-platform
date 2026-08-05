"""KBD 关键信号的只读领域边界。

KBD Signal 只描述事实采集、确定性判定或变量生产。处置/修复动作仍属于
KBD 正文的解决方案，不得进入 ``signals_json``。本模块刻意不实现通用 Shell
风险分析，只识别实际执行向量中的明确写命令、子命令与开关；LLM 错把“修复后
执行的只读验证”标成 solution 时，可由封闭只读命令表证明并纠正，不能误报为
write_signal。
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
        "on",
        "off",
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
        "cp",
        "mv",
        "sfscp",
        "touch",
        "truncate",
        "chmod",
        "chown",
        "mount",
        "umount",
    }
)
WRITE_OPERATION_FLAGS = frozenset({"--off", "--on"})
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


def _command_executable(command: str) -> str:
    """返回 command 首个 token 的 basename；参数不参与可执行程序判定。"""

    parts = [token for token in re.split(r"\s+", command.strip().lower()) if token]
    return parts[0].rstrip("/").rsplit("/", 1)[-1] if parts else ""


def kbd_signal_read_only_violation(
    signal: Any,
    *,
    allow_read_only_solution_correction: bool = False,
) -> str | None:
    """返回 KBD Signal 的只读边界违规原因；合法时返回 ``None``。

    只有 LLM Candidate 抽取入口可显式开启只读 solution 纠偏；专家保存、发布与
    Agent 运行必须使用默认严格模式，防止未归一的处置阶段 Signal 进入执行图。
    """

    if not isinstance(signal, dict):
        return None

    write_command = signal_write_operation_command(signal)
    if write_command:
        return f"检测到写操作命令 {write_command}；{_REMEDIATION_GUIDANCE}"

    orchestrate = signal.get("orchestrate") or {}
    if (
        str(orchestrate.get("phase") or "diagnostic") == "solution"
        and not (
            allow_read_only_solution_correction
            and signal_explicitly_read_only_command(signal)
        )
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
        return _command_executable(command) in READ_ONLY_SYSTEM_COMMANDS
    return tokens[-1] in READ_ONLY_SUBCOMMAND_SUFFIXES


def signal_write_operation_command(signal: Any) -> str | None:
    """返回实际执行向量中命中的明确写动作；不做通用 Shell 推断。"""

    if not isinstance(signal, dict):
        return None
    acquire = signal.get("acquire") or {}
    tool = str(acquire.get("tool") or "")
    if not tool.startswith("qfk_"):
        return None
    args = acquire.get("args") or {}
    command = str(args.get("command") or "").strip().lower()
    action = str(args.get("action") or "").strip().lower()
    if action in WRITE_OPERATION_COMMANDS:
        return action
    command_parts = [token for token in re.split(r"\s+", command) if token]
    executable = _command_executable(command)
    if executable in WRITE_OPERATION_COMMANDS:
        return executable

    if tool != "qfk_system":
        tokens = {token for token in re.split(r"[\s|/]+", command) if token}
        write_tokens = sorted(tokens & WRITE_OPERATION_COMMANDS)
        return write_tokens[0] if write_tokens else None

    # 封闭只读命令的参数是被读取的对象或筛选条件，不是被执行的子命令。
    # 例如 ``ls -l /sf/bin/sfscp`` 只查看文件，不能因路径 basename 命中
    # ``sfscp`` 就判为写操作；strace 等 wrapper 不在只读表中，仍继续扫描 argv。
    if tool == "qfk_system" and executable in READ_ONLY_SYSTEM_COMMANDS:
        return None

    # qfk_system 将子命令、开关和被 strace 等包装器执行的程序放在
    # command_args。只匹配完整参数或路径 basename，避免把正文关键字当动作。
    command_args = args.get("command_args") or []
    if not isinstance(command_args, list):
        return None
    for item in [*command_parts[1:], *command_args]:
        argument = str(item).strip().lower()
        if argument in WRITE_OPERATION_FLAGS:
            return argument
        basename = argument.rstrip("/").rsplit("/", 1)[-1]
        if basename in WRITE_OPERATION_COMMANDS:
            return basename
    return None


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
