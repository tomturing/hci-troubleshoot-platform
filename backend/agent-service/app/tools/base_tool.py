"""
工具基础定义

ToolDefinition：描述单个工具的元数据模型，供工具注册表（TOOL_REGISTRY）使用。

风险等级（risk_level）：
  1 = 只读操作，自动执行（policy: auto / notify）
  2 = 写操作，需用户确认后执行（policy: confirm）
  3 = 高危操作，直接 block 拒绝执行（policy: block）

策略（policy）：
  auto    : 自动执行，无需通知
  notify  : 执行前向前端发送通知（如日志获取）
  confirm : 需要用户确认后执行（写操作）
  block   : 拒绝执行（高危操作）

类别（category）：
  scp     : SCP 平台 REST API（查询告警、任务、虚拟机等）
  acli    : acli 命令行工具（节点级诊断和操作）
  sop     : SOP 导航工具（get_sop_node、sop_advance 等）
"""

from pydantic import BaseModel


class ToolDefinition(BaseModel):
    """工具定义（OpenAI function calling 格式 + 扩展字段）

    此类是所有 agent 工具注册表的基础数据模型，
    供 htp、ops、pai 等各 agent 的 TOOL_REGISTRY 使用。
    """

    name: str
    description: str
    parameters: dict  # JSON Schema
    risk_level: int  # 1=只读, 2=写操作需确认, 3=高危禁用
    policy: str  # auto|notify|confirm|block
    category: str  # scp|acli|sop|...
    usage_template: str | None = None  # ACLI 插件工具命令模板（可为空）
