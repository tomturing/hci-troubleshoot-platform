"""
RiskClassifier：基于规则对 acli/bash 命令进行动态风险分级。
规则按 risk 降序排列，第一个匹配的规则获胜。

权威来源：docs/solution/agent/agent工具设计.md §六.2
"""
import re

# acli 风险规则（risk 从高到低匹配，第一个命中获胜）
# acli 命令有两种格式：acli vm start abc-123 或 acli vm abc-123 start
_ACLI_RISK_RULES: list[tuple[int, re.Pattern]] = [
    # risk=3：破坏性操作（block）
    (3, re.compile(r"acli\s+vm\s+delete\b")),
    (3, re.compile(r"acli\s+storage\s+\S+\s+\S*\s*(delete|remove|wipe|format|destroy)\b")),
    (3, re.compile(r"acli\s+network\s+\S+\s*(delete|remove)\b")),
    (3, re.compile(r"acli\s+system\s+(rm|del|format)\b")),
    # risk=2：有副作用的写操作（confirm）
    # 支持两种格式：acli vm start <id> 或 acli vm <id> start
    (2, re.compile(r"acli\s+(vm|service|platform)\s+(start|stop|shutdown|restart|suspend|resume)\b")),
    (2, re.compile(r"acli\s+(vm|service|platform)\s+\S+\s+(start|stop|shutdown|restart|suspend|resume)\b")),
    (2, re.compile(r"acli\s+service\s+\S+\s+\S+\s+(restart|start|stop)\b")),
    (2, re.compile(r"acli\s+network\s+nic\s+(up|down|set)\b")),
    (2, re.compile(r"acli\s+vm\s+(migrate|clone|snapshot)\b")),
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
    (2, re.compile(r"(>>|>\s*/[a-z]|\btee\b|\bsed\s+-i\b|echo\s+.*>\s*[^>])")),
    # risk=1：默认，无需规则
]


def classify_acli(command: str | None) -> int:
    """
    对 acli 命令进行风险分级。

    Args:
        command: acli 命令字符串（None 或空字符串视为只读）

    Returns:
        1 (auto) | 2 (confirm) | 3 (block)

    Examples:
        >>> classify_acli("acli vm list --formatter json")
        1
        >>> classify_acli("acli service asv redis restart")
        2
        >>> classify_acli("acli vm delete abc-123")
        3
    """
    if not command:
        return 1  # None 或空命令视为只读

    for risk, pattern in _ACLI_RISK_RULES:
        if pattern.search(command):
            return risk

    return 1  # 未命中任何规则，默认只读


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
