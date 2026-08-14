"""
Fact and ClaimEvidenceLink Database Models
排障事实与断言证据链 ORM 模型 (T4-3)
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Numeric, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship

from shared.database.postgres import Base
from shared.models.base import TraceableMixin


class Fact(Base, TraceableMixin):
    """排障事实表"""

    __tablename__ = "fact"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    case_id = Column(String(20), nullable=False, index=True, comment="关联工单 ID")
    fact_type = Column(String(50), nullable=False, index=True, comment="事实类型")
    key = Column(String(100), nullable=False, index=True, comment="事实键名")
    source = Column(String(50), nullable=False, comment="事实来源")
    raw_ref = Column(Text, nullable=True, comment="原始引用ID")
    normalized_value = Column(JSONB, nullable=False, comment="标准化JSON数据")
    confidence = Column(Numeric(4, 3), nullable=False, default=1.0, comment="置信度")
    freshness = Column(String(30), nullable=False, default="unknown", comment="时效性")
    conflict = Column(Boolean, nullable=False, default=False, comment="是否存在冲突")
    collected_at = Column(DateTime(timezone=True), nullable=True, comment="数据实际采集时间")
    created_at = Column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False, comment="记录创建时间"
    )

    # 建立与 EvidenceLink 的关系
    evidence_links = relationship("ClaimEvidenceLink", back_populates="fact", cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return f"<Fact(id={self.id}, case_id={self.case_id}, fact_type={self.fact_type}, key={self.key})>"


class ClaimEvidenceLink(Base):
    """结论与事实证据链关联表"""

    __tablename__ = "claim_evidence_link"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    case_id = Column(String(20), nullable=False, index=True, comment="关联工单 ID")
    claim_id = Column(String(50), nullable=False, index=True, comment="断言/结论 ID")
    fact_id = Column(
        String(36), ForeignKey("fact.id", ondelete="CASCADE"), nullable=False, index=True, comment="关联事实 ID"
    )
    relation = Column(String(30), nullable=False, comment="关联关系: supporting/contradicting")
    confidence = Column(Numeric(4, 3), nullable=False, default=1.0, comment="相关置信度")
    created_at = Column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False, comment="记录创建时间"
    )

    # 建立与 Fact 的关系
    fact = relationship("Fact", back_populates="evidence_links")

    def __repr__(self) -> str:
        return f"<ClaimEvidenceLink(id={self.id}, case_id={self.case_id}, claim_id={self.claim_id}, relation={self.relation})>"
