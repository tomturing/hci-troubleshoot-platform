"""
Case数据模型
"""

from sqlalchemy import Column, String, Text, Enum as SQLEnum, DateTime
from sqlalchemy.dialects.postgresql import UUID
import uuid
import enum
from datetime import datetime

import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../..')))

from shared.database.postgres import Base
from shared.models.base import TimestampMixin, TraceableMixin

class CaseStatus(str, enum.Enum):
    """工单状态枚举"""
    CREATED = "created"
    CONFIRMED = "confirmed"
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"
    CLOSED = "closed"
    CANCELLED = "cancelled"

class Case(Base, TimestampMixin, TraceableMixin):
    """工单表"""
    
    __tablename__ = "cases"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    case_id = Column(String(50), unique=True, nullable=False, index=True)
    client_id = Column(String(100), nullable=False, index=True)
    status = Column(SQLEnum(CaseStatus), default=CaseStatus.CREATED, nullable=False)
    title = Column(String(200), nullable=False)
    description = Column(Text)
    closed_at = Column(DateTime, nullable=True)
    
    def __repr__(self):
        return f"<Case(case_id={self.case_id}, status={self.status})>"
