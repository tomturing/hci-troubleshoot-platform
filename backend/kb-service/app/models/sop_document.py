"""
KB Service SQLAlchemy 模型 — sop_document

对应数据库表：sop_document（SOP 排障手册文档）
生命周期：draft → published → archived
"""

from datetime import UTC, datetime

from shared.database.postgres import Base
from sqlalchemy import Column, DateTime, Integer, String, Text
from sqlalchemy.orm import relationship


class SopDocument(Base):
    """SOP 排障手册文档模型

    状态机：draft → published → archived
    source_id：幂等键，格式如 sop-vm-start-failure（对应 SOP 文档内部标识）
    """

    __tablename__ = "sop_document"

    id = Column(Integer, primary_key=True, autoincrement=True)
    source_id = Column(String(100), unique=True, nullable=True)            # 幂等键，如 sop-vm-start-failure
    category_id = Column(String(32), nullable=True)                        # 关联 KB 分类（kb_category.code）
    title = Column(String(500), nullable=False)                            # SOP 标题
    content_md = Column(Text, nullable=False)                              # 完整 SOP Markdown
    docx_hash = Column(String(64), nullable=True)                          # 源文件哈希（幂等去重）
    status = Column(String(20), default="draft", nullable=False)           # draft/published/archived
    reviewer_id = Column(Integer, nullable=True)                           # 审核人 ID
    reviewed_at = Column(DateTime(timezone=True), nullable=True)           # 审核时间
    published_at = Column(DateTime(timezone=True), nullable=True)          # 发布时间
    # 命中统计（case 级去重，物化列）
    hit_count = Column(Integer, nullable=False, default=0)                 # 有多少个唯一 case 命中此 SOP（S1 命中时 +1）

    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )

    # 关联（级联删除 chunks）
    chunks = relationship("SopChunk", back_populates="document", cascade="all, delete-orphan", lazy="select")

    # 合法状态集合
    VALID_STATUSES = frozenset({"draft", "published", "archived"})

    def __repr__(self) -> str:
        return f"<SopDocument(id={self.id}, title={self.title[:30]}..., status={self.status})>"
