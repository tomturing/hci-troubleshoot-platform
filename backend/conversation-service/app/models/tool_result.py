"""
ToolResult Model - 工具执行记录表

从 audit_log 拆分，专用于记录 ReAct 执行器每次工具调用的完整信息。

解决 BUG-03：
  原 tool_audit_log（already dropped by migration 20260402003）缺少 step_no 字段，
  导致无法追踪工具调用与 S3 验证步骤的对应关系。

注意：ToolResult 模型定义在 shared/models/audit.py（共享层），此处仅做重导出。
  避免与 shared 层的定义形成双重注册导致 SQLAlchemy 表冲突（Table 'tool_result' already defined）。
"""

# 从 shared 层重导出（不重复定义 __tablename__）
from shared.models.audit import ToolResult  # noqa: F401

__all__ = ["ToolResult"]
