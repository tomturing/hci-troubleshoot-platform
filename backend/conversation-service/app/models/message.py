"""
Message Model
"""

from sqlalchemy import Column, String, Text, Enum as SQLEnum, DateTime, ForeignKey
from sqlalchemy.dialects.postgresql import UUID, JSONB
import uuid
import enum
from datetime import datetime

import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../..')))

from shared.database.postgres import Base
from shared.models.base import TimestampMixin, TraceableMixin

class MessageRole(str, enum.Enum):
    """消息角色枚举"""
    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    COMMAND = "command"

class Message(Base, TimestampMixin, TraceableMixin):
    """消息表"""
    
    __tablename__ = "messages"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    message_id = Column(UUID(as_uuid=True), unique=True, nullable=False, default=uuid.uuid4, index=True)
    conversation_id = Column(UUID(as_uuid=True), ForeignKey("conversations.conversation_id"), nullable=False, index=True)
    role = Column(SQLEnum(MessageRole), nullable=False)
    content = Column(Text, nullable=False)
    metadata = Column(JSONB, nullable=True)
    
    def __repr__(self):
        return f"<Message(message_id={self.message_id}, role={self.role})>"
