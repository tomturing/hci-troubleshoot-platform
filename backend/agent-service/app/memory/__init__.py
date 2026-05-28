"""
Agent 工作记忆模块

实现 MemGPT 外化工作记忆理论（见 docs/solution/agent/agent记忆设计.md §5.1）。

工作记忆以 PostgreSQL 的 sop_execution.context_variables JSONB 字段存储，
每轮 LLM 调用时渲染注入上下文窗口，实现"无限上下文"的外化记忆。

子模块：
  - variable_pool: 变量池管理（SOP 上下文变量的 JIT 获取引擎）

变量赋值策略（acquisition_strategy）：
  - env_injection   : 环境注入，初始化阶段批量写入（如 node_ip、cluster_name）
  - user_input      : 用户输入，JIT 阻塞等待用户提供值
  - user_confirm    : 用户确认，JIT 展示候选值让用户选择
  - tool_call       : 工具调用，JIT 自动调用工具获取值（如 get_vm_list）
  - llm_inference   : LLM 推断，通过 sop_advance.variables_extracted 被动写入
  - agent_pass      : Agent 传递，初始化或编程方式写入
"""

from app.memory.variable_pool import VariableRequestResult, sop_request_variable

__all__ = ["VariableRequestResult", "sop_request_variable"]
