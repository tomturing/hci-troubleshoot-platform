"""
Message Model - 消息表
"""

import enum
import uuid
from datetime import UTC, datetime

from shared.database.postgres import Base
from shared.models.base import TraceableMixin
from sqlalchemy import Column, DateTime, ForeignKey, String, Text
from sqlalchemy import Enum as SQLEnum
from sqlalchemy.dialects.postgresql import JSONB, UUID


class MessageRole(enum.StrEnum):
    """消息角色枚举"""

    user = "user"
    assistant = "assistant"
    system = "system"
    command = "command"


class Message(Base, TraceableMixin):
    """消息表"""

    __tablename__ = "message"

    message_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conversation_id = Column(
        UUID(as_uuid=True), ForeignKey("conversation.conversation_id", ondelete="CASCADE"), nullable=False, index=True
    )
    case_id = Column(String(20), nullable=False, index=True)
    role = Column(SQLEnum(MessageRole, name="message_role"), nullable=False)
    content = Column(Text, nullable=False)
    command = Column(Text, nullable=True)
    command_warning = Column(Text, nullable=True)
    metadata_ = Column("metadata", JSONB, default=dict)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False)

    def __repr__(self):
        return f"<Message(message_id={self.message_id}, role={self.role}, case_id={self.case_id})>"
