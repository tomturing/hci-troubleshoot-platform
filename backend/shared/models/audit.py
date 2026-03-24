"""
工具调用审计日志 ORM 模型

对应数据库表：tool_audit_log
记录 ReAct 执行器每次工具调用的完整信息，支持：
  - 安全审计：谁发起了哪个工具调用，风险等级，结果
  - 性能分析：duration_ms 统计各工具耗时
  - 问题排查：error 字段记录工具执行失败信息
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import Column, DateTime, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB

from ..database.postgres import Base


class ToolAuditLog(Base):
    """工具调用审计日志模型"""

    __tablename__ = "tool_audit_log"

    # 主键：由 ReactExecutor 生成的 UUID，确保全局唯一
    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))

    # 会话关联
    session_id = Column(String(36), nullable=False, index=True)

    # 工具信息
    tool_name = Column(String(100), nullable=False, index=True)
    tool_args = Column(JSONB, nullable=True)          # 工具调用参数（JSON）

    # 风险控制
    risk_level = Column(Integer, nullable=False)       # 1=只读, 2=写操作, 3=高危
    policy = Column(String(20), nullable=False)        # auto|notify|confirm|block
    authorized_by = Column(String(100), nullable=True) # risk_level>=2 时记录确认人

    # 执行结果
    result = Column(JSONB, nullable=True)              # 执行结果摘要（截断到 2000 字符）
    error = Column(Text, nullable=True)                # 执行异常信息

    # 时间统计
    started_at = Column(DateTime(timezone=True), nullable=False)
    completed_at = Column(DateTime(timezone=True), nullable=False)
    duration_ms = Column(Integer, nullable=True)       # 执行耗时（毫秒）

    # 链路追踪
    trace_id = Column(String(64), nullable=True, index=True)

    # 记录创建时间（兜底，优先用 started_at）
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )

    def __repr__(self) -> str:
        return (
            f"<ToolAuditLog(id={self.id}, tool={self.tool_name}, "
            f"session={self.session_id}, risk={self.risk_level})>"
        )
