"""
SopExecution Model - SOP 执行状态表

存储 SOP 决策树执行过程的状态，支持中断恢复和并发多工单。

字段说明：
  - conversation_id: 1:1 关联会话（唯一约束）
  - sop_document_id: 关联 SOP 文档
  - current_node_id: 当前决策树节点 ID
  - status: 执行状态（active/completed/interrupted/aborted）
  - context_variables: 运行时变量池
  - completed_steps: 已完成节点列表（防止恢复后写操作节点重复执行）
  - pending_variable_name: 待填变量名（interrupted 状态）
  - execution_log: 节点导航事件序列
"""

import uuid
from datetime import UTC, datetime

from shared.database.postgres import Base
from shared.models.base import TraceableMixin
from sqlalchemy import Column, DateTime, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import JSONB, UUID

# status 值域常量
STATUS_ACTIVE = "active"
STATUS_COMPLETED = "completed"
STATUS_INTERRUPTED = "interrupted"
STATUS_ABORTED = "aborted"


class SopExecution(Base, TraceableMixin):
    """SOP 执行状态表"""

    __tablename__ = "sop_execution"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conversation_id = Column(
        UUID(as_uuid=True),
        ForeignKey("conversation.conversation_id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    sop_document_id = Column(Integer, nullable=False)
    current_node_id = Column(String(64), nullable=False)
    status = Column(String(16), nullable=False, default=STATUS_ACTIVE)
    context_variables = Column(JSONB, nullable=False, default=dict)
    completed_steps = Column(JSONB, nullable=False, default=list)
    pending_variable_name = Column(String(64), nullable=True)
    execution_log = Column(JSONB, nullable=False, default=list)
    trace_id = Column(String(64), nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )

    def __repr__(self) -> str:
        return (
            f"<SopExecution(id={self.id}, conversation_id={self.conversation_id}, "
            f"status={self.status!r}, current_node_id={self.current_node_id!r})>"
        )
