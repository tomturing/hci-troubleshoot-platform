"""qkv_vm_console 专用适配器包。

设计来源：docs/solution/agent/虚拟机控制台视觉生产者信号设计与需求.md。

⚠️ 本包是在线路径的唯一执行入口：KBD 差异诊断遇到 qkv_vm_console 信号时必须
路由到 ``adapter.run_vm_console_signal``，绝不落入自由文本 qkv_exec 路径。
"""

from app.tools.vm_console.adapter import (
    VmConsoleCaptureResult,
    run_vm_console_signal,
    run_wake_and_recapture,
)

__all__ = [
    "VmConsoleCaptureResult",
    "run_vm_console_signal",
    "run_wake_and_recapture",
]
