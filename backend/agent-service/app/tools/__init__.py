"""
Agent 公共工具模块

提供所有 agent（htp、ops、pai）共用的工具基础设施。

--------------------------------------------------------------------------------
🎯 理想微内核演进蓝图 & 方案一（逻辑外观层软解耦）设计规范：
本模块遵循“控制与执行解耦的微内核架构（Microkernel Architecture）”：
1. 物理层保持工程的高稳定性：保留原有的 tools/acli/ 和 tools/sop/ 等子包。
2. 逻辑外观层（Facade）实现 100% 理想微内核语义对齐：
   向外显式暴露出 tools.SystemTools 与 tools.InteractiveTools 逻辑命名空间，
   使得 variable_pool 变量池 JIT 决策内核可以无视底层的技术实现细节，仅依循认知语义进行调用：
   - SystemTools (系统级无状态工具): 对标 `tool_call` 策略（底包: acli/）
   - InteractiveTools (交互与状态流转工具): 对标 `user_confirm` 与 `user_input` 策略（底包: sop/）

后续迭代指导：
- 当开发人员或 AI 智能体新增工具或执行器时，应优先在 SystemTools 或 InteractiveTools
  中注册逻辑门面，使 JIT 控制引擎单向依赖此 Facade。
- 这有助于在未来的物理重构阶段，无缝地向“最终完美方案（Option 2 物理分拆）”平滑演化。
--------------------------------------------------------------------------------
"""

from app.tools.base_tool import ToolDefinition


class SystemTools:
    """系统级主动命令类工具（逻辑命名空间）

    对标变量池的 `tool_call` 策略，封装无状态执行动作（ACLI/Bash 命令等）。
    """

    @staticmethod
    def get_acli_exec():
        from app.tools.acli.executor import acli_exec
        return acli_exec

    @staticmethod
    def get_bash_exec():
        from app.tools.acli.executor import bash_exec
        return bash_exec


class InteractiveTools:
    """交互类/状态流转类工具（逻辑命名空间）

    对标变量池的 `user_confirm` 与 `user_input` 策略，提供人机交互、选项生成与 SOP 状态跳转动作。
    """

    @staticmethod
    def get_sop_client_class():
        from app.tools.sop.client import ConversationSopClient
        return ConversationSopClient

    @staticmethod
    def get_sop_nav_helpers():
        from app.tools.sop.nav import get_sop_node, sop_advance
        return get_sop_node, sop_advance


__all__ = ["ToolDefinition", "SystemTools", "InteractiveTools"]
