"""
Conversation Model - 对话会话表（只读视图，用于 QualityScoreService 查询）
"""

import uuid
from datetime import UTC, datetime

from shared.database.postgres import Base
from sqlalchemy import Column, DateTime, Integer, String
from sqlalchemy.dialects.postgresql import JSONB, UUID


class Conversation(Base):
    """对话会话表（只读视图）"""

    __tablename__ = "conversation"

    conversation_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    case_id = Column(String(20), nullable=False, index=True)
    pod_id = Column(String(100), nullable=True)
    assistant_type = Column(String(50), nullable=False, default="openclaw")
    started_at = Column(DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False)
    ended_at = Column(DateTime(timezone=True), nullable=True)
    message_count = Column(Integer, default=0)
    metadata_ = Column("metadata", JSONB, default=dict)
    repeat_question_count = Column(Integer, default=0, nullable=True)  # 用户重复提问次数

    def __repr__(self):
        return f"<Conversation(conversation_id={self.conversation_id}, case_id={self.case_id})>"
