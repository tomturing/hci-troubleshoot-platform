"""
关键信号建模资产与异常复盘模型:
- SignalModelingTemplate: 13 类信号的输入 Schema 与参数契约
- SignalBestPractice: 专家最终审核通过的黄金实例 (Few-Shot 检索)
- SignalFailureExtraction: 计数/分类/建模/验证各阶段异常抽取复盘表
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from shared.database.postgres import Base
from sqlalchemy import ARRAY, BigInteger, Boolean, Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB


class SignalModelingTemplate(Base):
    """信号类型建模标准模板库"""

    __tablename__ = "signal_modeling_template"

    id = Column(Integer, primary_key=True, autoincrement=True)
    tool_name = Column(String(32), unique=True, nullable=False)
    category = Column(String(16), nullable=False)  # frontend / backend
    description = Column(Text, nullable=False)
    acquire_schema = Column(JSONB, nullable=False, default=dict)
    allowed_matcher_types = Column(ARRAY(String(32)), nullable=False, default=list)
    variable_protocol = Column(JSONB, nullable=False, default=dict)
    anti_patterns = Column(ARRAY(Text), nullable=False, default=list)
    is_active = Column(Boolean, default=True, nullable=False)
    trace_id = Column(String(64), nullable=False, default=lambda: uuid4().hex)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC), nullable=False
    )


class SignalBestPractice(Base):
    """信号建模最佳实践库"""

    __tablename__ = "signal_best_practice"

    id = Column(Integer, primary_key=True, autoincrement=True)
    template_id = Column(Integer, ForeignKey("signal_modeling_template.id", ondelete="CASCADE"), nullable=True)
    tool_name = Column(String(32), nullable=False, index=True)
    pattern_category = Column(String(64), nullable=False)
    source_kbd_id = Column(BigInteger, ForeignKey("kbd_entry.id", ondelete="SET NULL"), nullable=True)
    support_id = Column(String(32), nullable=True)
    raw_evidence = Column(Text, nullable=False)
    signal_json = Column(JSONB, nullable=False, default=dict)
    design_notes = Column(Text, nullable=False, default="")
    completeness_score = Column(Integer, default=10, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    trace_id = Column(String(64), nullable=False, default=lambda: uuid4().hex)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC), nullable=False
    )


class SignalFailureExtraction(Base):
    """信号抽取异常复盘日志表"""

    __tablename__ = "signal_failure_extraction"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    kbd_id = Column(BigInteger, ForeignKey("kbd_entry.id", ondelete="CASCADE"), nullable=True, index=True)
    stage = Column(String(32), nullable=False, index=True)  # count, classify, modeling, verification
    raw_content = Column(Text, nullable=False)
    reason = Column(String(64), nullable=False)
    detail_payload = Column(JSONB, nullable=False, default=dict)
    trace_id = Column(String(64), nullable=False, index=True, default=lambda: uuid4().hex)
    resolved = Column(Boolean, default=False, nullable=False)
    resolved_by = Column(String(64), nullable=True)
    resolved_notes = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC), nullable=False
    )
