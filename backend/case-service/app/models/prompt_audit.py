"""
PromptAudit Model - AI 层入口 Prompt 审计镜像表
"""

import uuid
from datetime import UTC, datetime

from shared.database.postgres import Base
from shared.models.base import TraceableMixin
from sqlalchemy import Boolean, Column, DateTime, Float, ForeignKey, Integer, SmallInteger, String
from sqlalchemy.dialects.postgresql import JSONB, UUID


class PromptAudit(Base, TraceableMixin):
    """AI 层入口 Prompt 审计镜像表"""

    __tablename__ = "prompt_audit"

    audit_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conversation_id = Column(
        UUID(as_uuid=True), ForeignKey("conversation.conversation_id", ondelete="CASCADE"), nullable=False
    )
    case_id = Column(String(20), nullable=True)

    # AI 助手信息
    assistant_type = Column(String(50), nullable=True)
    model = Column(String(100), nullable=True)

    # 元数据（小字段，~200 bytes/条，100% 覆盖采集）
    message_count = Column(Integer, nullable=True)
    has_sop = Column(Boolean, default=False, nullable=True)
    kb_chunks_count = Column(Integer, default=0, nullable=True)
    kb_top_score = Column(Float, default=0.0, nullable=True)
    system_prompt_chars = Column(Integer, nullable=True)

    # 完整 payload（大字段，按策略采样存储）
    messages = Column(JSONB, nullable=True)
    payload_ref = Column(String(200), nullable=True)

    # 关联回路（用户评分后回填）
    user_rating = Column(SmallInteger, nullable=True)

    # 审计字段
    captured_at = Column(DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=True)

    def __repr__(self):
        return f"<PromptAudit(audit_id={self.audit_id}, case_id={self.case_id})>"
