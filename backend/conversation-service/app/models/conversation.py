"""
Conversation Model - 对话会话表
"""

from sqlalchemy import Column, String, Integer, DateTime, ForeignKey
from sqlalchemy.dialects.postgresql import UUID, JSONB
import uuid
from datetime import datetime, timezone

from shared.database.postgres import Base
from shared.models.base import TraceableMixin


class Conversation(Base, TraceableMixin):
    """对话会话表"""
    
    __tablename__ = "conversation"
    
    conversation_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    case_id = Column(String(20), nullable=False, index=True)
    pod_id = Column(String(100), nullable=True)
    assistant_type = Column(String(50), nullable=False, default="openclaw")
    started_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    ended_at = Column(DateTime(timezone=True), nullable=True)
    message_count = Column(Integer, default=0)
    metadata_ = Column("metadata", JSONB, default=dict)
    
    def __repr__(self):
        return f"<Conversation(conversation_id={self.conversation_id}, case_id={self.case_id})>"
