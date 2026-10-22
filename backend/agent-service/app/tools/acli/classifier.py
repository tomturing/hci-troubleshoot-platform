"""
RiskClassifier：基于规则对 acli/bash 命令进行动态风险分级。
规则按 risk 降序排列，第一个匹配的规则获胜。

权威来源：docs/solution/agent/agent工具设计.md §六.2
"""

import re

# acli 风险规则（risk 从高到低匹配，第一个命中获胜）
# acli 命令有两种格式：acli vm start abc-123 或 acli vm abc-123 start
#
# 重要：qfk_system / qfk_vm 等后端信号会把 command 直接拼成
#   `acli {namespace} {command}`  （见 qfk/handlers.GenericSubCommandHandler）
# 若 command 本身是写操作（如 `kill -9 <PID>`、`reboot`、`rm -rf`），命令会被
# BridgeRelayExecutor 重新用本分类器求值（executor.py:468 classify_acli）。因此必须在此
# 显式覆盖「系统子命令中包裹的非只读动作」，否则会被默认判为 risk=1 静默自动执行（安全倒挂）。
_ACLI_RISK_RULES: list[tuple[int, re.Pattern]] = [
    # risk=3：破坏性操作（block）
    (3, re.compile(r"acli\s+vm\s+delete\b")),
    (3, re.compile(r"acli\s+storage\s+\S+\s+\S*\s*(delete|remove|wipe|format|destroy)\b")),
    (3, re.compile(r"acli\s+network\s+\S+\s*(delete|remove)\b")),
    # 日志收集取消会 kill 全集群采集进程并删除已收集的包，按破坏性动作处理。
    (3, re.compile(r"acli\s+log_collect\s+cancel\b")),
    # 系统子命令中包裹的破坏性非 acli 写操作（如 acli system rm -rf / mkfs / dd if=）
    (3, re.compile(r"acli\s+system\b.*\b(rm\s+-rf|mkfs|fdisk|parted|format|wipe|dd\s+if=)\b")),
    (3, re.compile(r"acli\s+system\s+(rm|del|format)\b")),
    # risk=2：有副作用的写操作（confirm）
    # 日志收集会在集群所有节点拉起采集进程并写盘，必须人工确认。
    (2, re.compile(r"acli\s+log_collect\s+create\b")),
    # 支持两种格式：acli vm start <id> 或 acli vm <id> start
    (2, re.compile(r"acli\s+(vm|service|platform)\s+(start|stop|shutdown|restart|suspend|resume)\b")),
    (2, re.compile(r"acli\s+(vm|service|platform)\s+\S+\s+(start|stop|shutdown|restart|suspend|resume)\b")),
    (2, re.compile(r"acli\s+service\s+\S+\s+\S+\s+(restart|start|stop)\b")),
    (2, re.compile(r"acli\s+network\s+nic\s+(up|down|set)\b")),
    (2, re.compile(r"acli\s+vm\s+(migrate|clone|snapshot)\b")),
    # 系统子命令中包裹的写操作：进程终止 / 重启 / 关机
    (2, re.compile(r"acli\s+system\b.*\b(kill|killall|pkill|reboot|shutdown|halt|poweroff)\b")),
    # 系统子命令中包裹的写操作：服务启停 / 权限与文件变更
    (2, re.compile(r"acli\s+system\b.*\b(systemctl\s+(start|stop|restart|reload|disable|enable))\b")),
    (2, re.compile(r"acli\s+system\b.*\b(chmod|chown|mv|remove|delete)\b")),
    # risk=1：只读操作（auto）—— 默认，无需规则
]

# bash 风险规则（risk 从高到低匹配，第一个命中获胜）
_BASH_RISK_RULES: list[tuple[int, re.Pattern]] = [
    # risk=3：破坏性操作（block）
    (3, re.compile(r"\b(rm\s+(-[rf]+\s+|--[a-z]+\s+)*\/|rm\s+-rf)\b")),
    (3, re.compile(r"\b(mkfs|fdisk|parted|format)\b")),
    (3, re.compile(r"\bdd\s+if=")),  # dd if= 不需要 \b 边界
    (3, re.compile(r"\b(reboot|shutdown|halt|poweroff)\b")),
    (3, re.compile(r"\b(passwd|useradd|userdel|usermod|visudo|sudoers)\b")),
    # risk=2：写操作（confirm）
    (2, re.compile(r"\bsystemctl\s+(start|stop|restart|reload|disable|enable)\b")),
    (2, re.compile(r"\b(kill|killall|pkill)\b")),
    (2, re.compile(r"\bchmod\s+[0-7]*7[0-7]{2}\b")),  # chmod 777 类
    (2, re.compile(r"(>>|>\s*/(?!dev/null)[a-zA-Z]|\btee\b|\bsed\s+-i\b|echo\s+.*>\s*[^>])")),
    # risk=1：默认，无需规则
]


def _catalog_risk_level(command: str) -> int | None:
    """从 Catalog ``risk_hint`` 读取风险等级。

    只把 write/danger 映射为更高风险；read 或未命中返回 ``None``，交由原有
    正则与 fail-closed 逻辑判定。局部导入避免模块初始化环。
    """

    try:
        from shared.resolution.catalog import acli_risk_hint

        hint = acli_risk_hint(command.split())
    except Exception:
        # Catalog 不可读时不得影响主判定：保持原有 fail-closed 路径。
        return None
    return {"danger": 3, "write": 2}.get(hint or "")


def classify_acli(command: str | None) -> int:
    """
    对 acli 命令进行风险分级。

    Args:
        command: acli 命令字符串（None 或空字符串视为只读）

    Returns:
        1 (auto) | 2 (confirm) | 3 (block)

    Examples:
        >>> classify_acli("acli --formatter json vm list")
        1
        >>> classify_acli("acli service asv redis restart")
        2
        >>> classify_acli("acli vm delete abc-123")
        3
    """
    if not command:
        return 1  # None 或空命令视为只读
    stripped = command.strip()
    # 本函数只负责 aCLI 风险；前缀合法性由语义校验器处理。保持非 aCLI/空白输入的
    # 既有返回值，避免把分类器误用为通用命令校验器。
    if not stripped.startswith("acli ") and stripped != "acli":
        return 1

    for risk, pattern in _ACLI_RISK_RULES:
        if pattern.search(command):
            return risk

    # Catalog 的 risk_hint 只用于**提升**风险（write→2 / danger→3），绝不用于降低：
    # risk_hint=read 时继续走原有判定，避免一条错误的 read 标注把写命令放行。
    catalog_risk = _catalog_risk_level(stripped)
    if catalog_risk is not None:
        return catalog_risk

    # Catalog 未知不等于只读。开发/验证默认提升为 risk=2 走人工确认；生产可通过
    # ACLI_UNKNOWN_COMMAND_POLICY=deny 在语义校验阶段直接拒绝。局部导入避免模块初始化环。
    try:
        from app.tools.acli.semantic_validator import is_acli_command_in_catalog

        if not is_acli_command_in_catalog(stripped):
            return 2
    except Exception:
        # Catalog 不可读时 fail closed 到确认，不把未知命令静默当作 risk=1。
        return 2

    return 1


def classify_bash(command: str | None) -> int:
    """
    对 bash 命令进行风险分级。

    Args:
        command: bash 命令字符串（None 或空字符串视为只读）

    Returns:
        1 (auto) | 2 (confirm) | 3 (block)

    Examples:
        >>> classify_bash("df -h /")
        1
        >>> classify_bash("systemctl restart nginx")
        2
        >>> classify_bash("rm -rf /tmp/test")
        3
    """
    if not command:
        return 1  # None 或空命令视为只读

    for risk, pattern in _BASH_RISK_RULES:
        if pattern.search(command):
            return risk

    return 1  # 未命中任何规则，默认只读


def risk_to_policy(risk: int) -> str:
    """
    将风险等级映射为执行策略。

    Args:
        risk: 风险等级（1/2/3）

    Returns:
        'auto' | 'confirm' | 'block'

    Examples:
        >>> risk_to_policy(1)
        'auto'
        >>> risk_to_policy(2)
        'confirm'
        >>> risk_to_policy(3)
        'block'
    """
    return {1: "auto", 2: "confirm", 3: "block"}.get(risk, "block")
