"""
Agent 公共工具模块

提供所有 agent（htp、ops、pai）共用的工具基础设施。

工具是 LLM 在 ReAct 循环中通过 tool_call 调用的可执行单元。
每个工具有对应的：
  - 声明（ToolDefinition / JSON Schema）：LLM 可读的工具描述，见 htp/tool_registry.py
  - 实现（Python 函数）：工具被调用时执行的实际逻辑，见 tools/sop/

子模块：
  - base_tool: 工具基础定义（ToolDefinition 模型，供各 agent 的工具注册表使用）
  - sop : SOP 导航工具实现（get_sop_node、sop_advance 等）
  - acli: acli 命令执行器（BridgeRelayExecutor）
"""

from app.tools.base_tool import ToolDefinition

__all__ = ["ToolDefinition"]
