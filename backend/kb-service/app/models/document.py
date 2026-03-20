"""
KB Service SQLAlchemy 模型 — kb_document
"""

from datetime import UTC, datetime

from shared.database.postgres import Base
from sqlalchemy import ARRAY, Boolean, Column, DateTime, Integer, SmallInteger, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship


class KBDocument(Base):
    """知识库文档模型

    状态机：draft → under_review → approved → published
            draft/under_review → rejected
            published → archived

    注意：KB Service 使用独立模型，不直接引用 shared/models/kb.py，
    以便 KB Service 可以独立部署和演化。
    """

    __tablename__ = "kb_document"

    id = Column(Integer, primary_key=True, autoincrement=True)
    source_id = Column(String(50), unique=True, nullable=True)            # 原始案例 ID
    title = Column(String(500), nullable=False)
    product = Column(String(100), default="超融合HCI")
    content_md = Column(Text, nullable=False)                             # MD 全文
    content_hash = Column(String(64), nullable=True)                      # SHA256，变更检测
    yaml_meta = Column(JSONB, nullable=True)                              # LLM 增强的结构化元数据
    category_l1 = Column(String(100), nullable=True)                      # 一级分类
    category_l2 = Column(String(100), nullable=True)                      # 二级分类
    tags = Column(ARRAY(Text), nullable=True)                             # 标签数组
    judgment_logic = Column(Text, nullable=True)                          # 排查逻辑（中文）
    summary = Column(Text, nullable=True)                                 # 摘要（中文）
    difficulty = Column(SmallInteger, default=3)                          # 难度 1-5
    status = Column(String(20), default="draft", nullable=False)          # 状态机
    review_note = Column(Text, nullable=True)                             # 审核批注
    reviewer = Column(String(100), nullable=True)                         # 审核人
    reviewed_at = Column(DateTime(timezone=True), nullable=True)
    source_type = Column(String(20), default="kb", nullable=False)        # kb/sop/realtime
    has_images = Column(Boolean, default=False)
    verified_version = Column(String(50), nullable=True)
    trace_id = Column(String(64), nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )

    # 关联（lazy=dynamic 避免 N+1 查询）
    chunks = relationship("KBChunk", back_populates="document", cascade="all, delete-orphan", lazy="select")

    # 合法状态集合
    VALID_STATUSES = frozenset({"draft", "under_review", "approved", "published", "rejected", "archived"})
    # 合法来源类型
    VALID_SOURCE_TYPES = frozenset({"kb", "sop", "realtime"})

    def __repr__(self) -> str:
        return f"<KBDocument(id={self.id}, title={self.title[:30]}..., status={self.status})>"
