"""
acli 命令执行器模块

提供 BridgeRelayExecutor，通过 Redis → SSE → Frontend → terminal_bridge 路径
执行 acli 命令。

v2.0 架构说明：
  - HCI 节点在客户私网，云端服务器没有直连路由
  - 所有 acli/bash 工具调用的唯一可行路径：通过 terminal_bridge.exe 中转
  - 执行流程：Agent Service → Redis → SSE → Frontend → terminal_bridge → SSH → HCI
"""

from app.tools.acli.classifier import classify_acli, classify_bash, risk_to_policy
from app.tools.acli.executor import (
    BridgeRelayExecutor,
    CommandSanitizer,
    ShellResult,
    acli_exec,
    bash_exec,
    set_executor,
)

__all__ = [
    # 执行器
    "BridgeRelayExecutor",
    "set_executor",
    # 结果数据结构
    "ShellResult",
    # 命令净化器
    "CommandSanitizer",
    # 风险分类器
    "classify_acli",
    "classify_bash",
    "risk_to_policy",
    # 工具入口函数
    "acli_exec",
    "bash_exec",
]
