"""
Case数据模型
"""

import enum

from shared.database.postgres import Base
from shared.models.base import TimestampMixin, TraceableMixin
from sqlalchemy import Column, DateTime, String, Text
from sqlalchemy import Enum as SQLEnum
from sqlalchemy.dialects.postgresql import JSONB, UUID


class CaseStatus(enum.StrEnum):
    """工单状态枚举"""

    created = "created"
    confirmed = "confirmed"
    in_progress = "in_progress"
    resolved = "resolved"
    closed = "closed"
    cancelled = "cancelled"


class CloseReason(enum.StrEnum):
    """工单关闭原因枚举"""

    user_command = "user_command"  # 用户主动输入命令关闭
    timeout = "timeout"            # 超时自动关闭
    abandon = "abandon"            # 用户放弃/断开连接
    admin_close = "admin_close"    # 管理员强制关闭


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
    close_reason = Column(String(20), nullable=True)  # 关闭原因：user_command/timeout/abandon/admin_close

    def __repr__(self):
        return f"<Case(case_id={self.case_id}, status={self.status})>"
