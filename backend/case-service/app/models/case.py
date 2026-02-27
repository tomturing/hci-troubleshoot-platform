"""
Case数据模型
"""

from sqlalchemy import Column, String, Text, Enum as SQLEnum, DateTime
from sqlalchemy.dialects.postgresql import UUID, JSONB
import uuid
import enum
from datetime import datetime, timezone

from shared.database.postgres import Base
from shared.models.base import TimestampMixin, TraceableMixin


class CaseStatus(str, enum.Enum):
    """工单状态枚举"""
    created = "created"
    confirmed = "confirmed"
    in_progress = "in_progress"
    resolved = "resolved"
    closed = "closed"
    cancelled = "cancelled"


class Case(Base, TimestampMixin, TraceableMixin):
    """工单表"""
    
    __tablename__ = "case"
    
    case_id = Column(String(20), primary_key=True)
    user_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    client_id = Column(String(255), nullable=False, index=True)
    title = Column(String(500), nullable=False)
    description = Column(Text, nullable=True)
    status = Column(SQLEnum(CaseStatus, name="case_status"), default=CaseStatus.created, nullable=False)
    priority = Column(String(20), default="medium")
    category = Column(String(100), nullable=True)
    assistant_type = Column(String(50), nullable=False, default="openclaw")
    metadata_ = Column("metadata", JSONB, default=dict)
    confirmed_at = Column(DateTime(timezone=True), nullable=True)
    resolved_at = Column(DateTime(timezone=True), nullable=True)
    closed_at = Column(DateTime(timezone=True), nullable=True)
    
    def __repr__(self):
        return f"<Case(case_id={self.case_id}, status={self.status})>"
