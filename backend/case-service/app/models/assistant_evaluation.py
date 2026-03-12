"""
AssistantEvaluation Model - AI 助手评估表
"""

import uuid
from datetime import datetime

from shared.database.postgres import Base
from shared.models.base import TraceableMixin
from sqlalchemy import Column, DateTime, ForeignKey, Integer, SmallInteger, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID


class AssistantEvaluation(Base, TraceableMixin):
    """AI 助手评估表"""

    __tablename__ = "assistant_evaluation"

    evaluation_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    case_id = Column(String(20), ForeignKey("case.case_id", ondelete="CASCADE"), nullable=False)
    conversation_id = Column(UUID(as_uuid=True), ForeignKey("conversation.conversation_id", ondelete="SET NULL"), nullable=True)
    assistant_type = Column(String(50), nullable=False)
    score = Column(SmallInteger, nullable=True)  # 用户评分 1-5
    feedback = Column(Text, nullable=True)
    resolution_time_seconds = Column(Integer, nullable=True)
    message_count = Column(Integer, nullable=True)
    metadata_ = Column("metadata", JSONB, default=dict)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow, nullable=False)

    # 评分评价体系新增字段
    close_reason = Column(String(20), nullable=True)  # 冗余存储关闭原因
    session_duration_sec = Column(Integer, nullable=True)  # 会话时长（秒）
    repeat_question_count = Column(Integer, nullable=True)  # 用户重复提问次数
    composite_score = Column(SmallInteger, nullable=True)  # 综合质量分 0-100
    score_breakdown = Column(JSONB, nullable=True)  # 各维度详细分解 JSON
    calculated_at = Column(DateTime(timezone=True), nullable=True)  # 综合质量分计算时间

    def __repr__(self):
        return f"<AssistantEvaluation(evaluation_id={self.evaluation_id}, case_id={self.case_id})>"
