"""
SOP 发布阶段工具契约校验单元测试。
"""

from __future__ import annotations

from app.services.sop_parser import parse_sop_markdown
from app.services.sop_tool_contract_validator import validate_sop_tool_contract


def _parse_root(content: str):
    result = parse_sop_markdown(content)
    assert result.has_error is False
    assert result.root_nodes
    return result.root_nodes[0]


def test_warns_when_acli_command_not_in_catalog():
    root = _parse_root(
        """\
# 存储故障

## 磁盘异常

### 判断方法

acli命令行：
- acli storage disk list

### 解决方案

快速恢复：
- 观察磁盘告警

彻底解决方案：
- 更换异常磁盘
"""
    )

    issues = validate_sop_tool_contract(root)

    assert any(i.code == "sop_tool_acli_command_not_in_catalog" for i in issues)


def test_accepts_acli_command_in_catalog():
    root = _parse_root(
        """\
# 存储故障

## 磁盘异常

### 判断方法

acli命令行：
- acli storage asan disk list

### 解决方案

快速恢复：
- 观察磁盘告警

彻底解决方案：
- 更换异常磁盘
"""
    )

    issues = validate_sop_tool_contract(root)

    assert not issues


def test_warns_when_catalog_command_has_no_required_invocation_args():
    root = _parse_root(
        """\
# 磁盘故障

## SMART 检查

### 判断方法

acli命令行：
- acli system smartctl

### 解决方案

快速恢复：
- 观察磁盘状态

彻底解决方案：
- 根据检查结果处理
"""
    )

    issues = validate_sop_tool_contract(root)

    assert any(i.code == "sop_tool_acli_invocation_invalid" for i in issues)


def test_accepts_naked_bash_command_as_host_boundary():
    root = _parse_root(
        """\
# 磁盘寿命异常

## 系统盘寿命异常

### 前置检查

```bash
lsblk | grep boot
```

### 判断方法

acli命令行：
- acli storage asan disk list

### 解决方案

快速恢复：
- 更换异常磁盘

彻底解决方案：
- 联系硬件售后更换磁盘并观察告警恢复
"""
    )

    issues = validate_sop_tool_contract(root)

    assert not issues


def test_accepts_container_exec_command():
    root = _parse_root(
        """\
# 磁盘寿命异常

## 系统盘寿命异常

### 前置检查

```bash
container_exec -n vs-cp-manager -c "smartctl -a /dev/sda"
```

### 判断方法

acli命令行：
- acli storage asan disk list

### 解决方案

快速恢复：
- 更换异常磁盘

彻底解决方案：
- 联系硬件售后更换磁盘并观察告警恢复
"""
    )

    issues = validate_sop_tool_contract(root)

    assert not issues


def test_warns_when_container_exec_command_cannot_be_normalized():
    root = _parse_root(
        """\
# 磁盘寿命异常

## 系统盘寿命异常

### 前置检查

```bash
container_exec -n vs-cp-manager
```

### 判断方法

acli命令行：
- acli storage asan disk list

### 解决方案

快速恢复：
- 更换异常磁盘

彻底解决方案：
- 联系硬件售后更换磁盘并观察告警恢复
"""
    )

    issues = validate_sop_tool_contract(root)

    assert any(i.code == "sop_tool_command_parse_failed" for i in issues)


def test_warns_when_bash_command_contains_container_prefix():
    root = _parse_root(
        """\
# 磁盘寿命异常

## 系统盘寿命异常

### 前置检查

```bash
docker exec vs-cp-manager lsblk
```

### 判断方法

acli命令行：
- acli storage asan disk list

### 解决方案

快速恢复：
- 更换异常磁盘

彻底解决方案：
- 联系硬件售后更换磁盘并观察告警恢复
"""
    )

    issues = validate_sop_tool_contract(root)

    assert any(i.code == "sop_tool_bash_container_prefix_forbidden" for i in issues)
