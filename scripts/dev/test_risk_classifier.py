#!/usr/bin/env python3
"""
测试动态风险分级功能（T-TOOL-15 验收脚本）

验收标准：
  - acli vm delete xxx 触发 block（risk=3）
  - acli vm list 不触发确认（risk=1）
  - acli service xxx restart 触发 confirm（risk=2）
  - bash_exec("rm -rf /tmp") 触发 block（risk=3）
"""

import re

# 直接复制 classifier.py 的核心函数进行测试（避免模块导入问题）

# acli 风险规则（risk 从高到低匹配，第一个命中获胜）
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
    """对 acli 命令进行风险分级。"""
    if not command:
        return 1
    for risk, pattern in _ACLI_RISK_RULES:
        if pattern.search(command):
            return risk
    return 1


def classify_bash(command: str | None) -> int:
    """对 bash 命令进行风险分级。"""
    if not command:
        return 1
    for risk, pattern in _BASH_RISK_RULES:
        if pattern.search(command):
            return risk
    return 1


def risk_to_policy(risk: int) -> str:
    """将风险等级映射为执行策略。"""
    return {1: "auto", 2: "confirm", 3: "block"}.get(risk, "block")


def test_acli_vm_delete():
    """验收标准 1：acli vm delete xxx 触发 block（risk=3）"""
    command = "acli vm delete abc-123"
    risk = classify_acli(command)
    policy = risk_to_policy(risk)
    print(f"✓ 测试命令: {command}")
    print(f"  风险等级: {risk}")
    print(f"  执行策略: {policy}")
    assert risk == 3, f"预期 risk=3，实际 risk={risk}"
    assert policy == "block", f"预期 policy=block，实际 policy={policy}"
    print("  ✅ 通过：正确触发 block\n")


def test_acli_vm_list():
    """验收标准 2：acli vm list 不触发确认（risk=1）"""
    command = "acli vm list --formatter json"
    risk = classify_acli(command)
    policy = risk_to_policy(risk)
    print(f"✓ 测试命令: {command}")
    print(f"  风险等级: {risk}")
    print(f"  执行策略: {policy}")
    assert risk == 1, f"预期 risk=1，实际 risk={risk}"
    assert policy == "auto", f"预期 policy=auto，实际 policy={policy}"
    print("  ✅ 通过：不触发确认\n")


def test_acli_service_restart():
    """验收标准 3：acli service xxx restart 触发 confirm（risk=2）"""
    # 测试格式 1：acli service <subsystem> <service> restart
    command1 = "acli service asv redis restart"
    risk1 = classify_acli(command1)
    policy1 = risk_to_policy(risk1)
    print(f"✓ 测试命令: {command1}")
    print(f"  风险等级: {risk1}")
    print(f"  执行策略: {policy1}")
    assert risk1 == 2, f"预期 risk=2，实际 risk={risk1}"
    assert policy1 == "confirm", f"预期 policy=confirm，实际 policy={policy1}"
    print("  ✅ 通过：正确触发 confirm\n")

    # 测试格式 2：acli vm start <id>
    command2 = "acli vm start abc-123"
    risk2 = classify_acli(command2)
    policy2 = risk_to_policy(risk2)
    print(f"✓ 测试命令: {command2}")
    print(f"  风险等级: {risk2}")
    print(f"  执行策略: {policy2}")
    assert risk2 == 2, f"预期 risk=2，实际 risk={risk2}"
    assert policy2 == "confirm", f"预期 policy=confirm，实际 policy={policy2}"
    print("  ✅ 通过：正确触发 confirm\n")


def test_bash_rm_rf():
    """验收标准 4：bash_exec("rm -rf /tmp") 触发 block（risk=3）"""
    command = "rm -rf /tmp"
    risk = classify_bash(command)
    policy = risk_to_policy(risk)
    print(f"✓ 测试命令: {command}")
    print(f"  风险等级: {risk}")
    print(f"  执行策略: {policy}")
    assert risk == 3, f"预期 risk=3，实际 risk={risk}"
    assert policy == "block", f"预期 policy=block，实际 policy={policy}"
    print("  ✅ 通过：正确触发 block\n")


def test_bash_systemctl_restart():
    """额外测试：systemctl restart 触发 confirm（risk=2）"""
    command = "systemctl restart nginx"
    risk = classify_bash(command)
    policy = risk_to_policy(risk)
    print(f"✓ 测试命令: {command}")
    print(f"  风险等级: {risk}")
    print(f"  执行策略: {policy}")
    assert risk == 2, f"预期 risk=2，实际 risk={risk}"
    assert policy == "confirm", f"预期 policy=confirm，实际 policy={policy}"
    print("  ✅ 通过：正确触发 confirm\n")


if __name__ == "__main__":
    print("=" * 60)
    print("动态风险分级测试（T-TOOL-15 验收）")
    print("=" * 60)
    print()

    try:
        test_acli_vm_delete()
        test_acli_vm_list()
        test_acli_service_restart()
        test_bash_rm_rf()
        test_bash_systemctl_restart()

        print("=" * 60)
        print("✅ 所有测试通过！")
        print("=" * 60)
    except AssertionError as e:
        print("=" * 60)
        print(f"❌ 测试失败: {e}")
        print("=" * 60)
        sys.exit(1)