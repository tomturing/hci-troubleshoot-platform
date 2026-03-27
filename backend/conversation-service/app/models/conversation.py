"""
Conversation Model - 对话会话表
"""

import uuid
from datetime import UTC, datetime

from shared.database.postgres import Base
from shared.models.base import TraceableMixin
from sqlalchemy import Column, DateTime, Integer, String
from sqlalchemy.dialects.postgresql import JSONB, UUID


class Conversation(Base, TraceableMixin):
    """对话会话表"""

    __tablename__ = "conversation"

    conversation_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    case_id = Column(String(20), nullable=False, index=True)
    pod_id = Column(String(100), nullable=True)
    assistant_type = Column(String(50), nullable=False, default="openclaw")
    started_at = Column(DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False)
    ended_at = Column(DateTime(timezone=True), nullable=True)
    message_count = Column(Integer, default=0)
    repeat_question_count = Column(Integer, default=0, nullable=False)
    metadata_ = Column("metadata", JSONB, default=dict)

    # 诊断状态字段（Phase 2 新增，迁移 0003）
    diagnostic_stage = Column(String(8), default="S0", nullable=False, comment="诊断阶段 S0-S6")
    category_l1 = Column(String(100), nullable=True, comment="一级分类")
    category_l2 = Column(String(100), nullable=True, comment="二级分类")
    category_id = Column(String(32), nullable=True, comment="分类 ID")
    hypothesis = Column(JSONB, default=list, nullable=True, comment="当前假设列表")
    react_state = Column(JSONB, default=dict, nullable=True, comment="ReAct 状态快照")
    pending_confirm = Column(JSONB, nullable=True, comment="待确认工具调用")

    def __repr__(self):
        return f"<Conversation(conversation_id={self.conversation_id}, case_id={self.case_id}, stage={self.diagnostic_stage})>"
