"""
Conversation Model
"""

from sqlalchemy import Column, String, DateTime, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
import uuid
from datetime import datetime

import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../..')))

from shared.database.postgres import Base
from shared.models.base import TimestampMixin, TraceableMixin

class Conversation(Base, TimestampMixin, TraceableMixin):
    """对话表"""
    
    __tablename__ = "conversations"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conversation_id = Column(UUID(as_uuid=True), unique=True, nullable=False, default=uuid.uuid4, index=True)
    case_id = Column(String(50), nullable=False, index=True)
    started_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    ended_at = Column(DateTime, nullable=True)
    
    def __repr__(self):
        return f"<Conversation(conversation_id={self.conversation_id}, case_id={self.case_id})>"
