"""
KB Service SQLAlchemy 模型 — kbd_entry

KBD 知识条目表，用于存储深信服案例原始数据。
生命周期：draft → published → archived / rejected
"""

from datetime import UTC, datetime

from shared.database.postgres import Base
from sqlalchemy import BigInteger, Column, DateTime, Float, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB


class KbdEntry(Base):
    """KBD 知识条目模型

    状态机：draft → published → archived / rejected

    字段说明：
    - support_id: 深信服案例ID（幂等键，唯一）
    - content_md: 结构化 Markdown 内容（审核通过后生成 embedding）
    - category_id: 人工确认的分类（引用 kb_category.code）
    - ai_category_id: AI 分类建议（由流水线提供）
    - embedding: 语义向量（审核通过时生成，1536 维）
    - tsv: BM25 全文索引（审核通过时生成）
    """

    __tablename__ = "kbd_entry"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    support_id = Column(String(20), unique=True, nullable=False)         # 深信服案例ID（幂等键）
    support_url = Column(Text, nullable=True)                            # 原始案例 URL
    title = Column(Text, nullable=False)                                 # 案例标题
    content_md = Column(Text, nullable=True)                             # 结构化 Markdown
    # 使用 entry_metadata 作为 Python 属性名，"metadata" 作为数据库列名
    # 避免 SQLAlchemy Base.metadata 保留属性冲突
    entry_metadata = Column("metadata", JSONB, nullable=False, default=dict)  # 补充元数据

    # 分类字段（双轨制）
    category_id = Column(String(32), nullable=True)                      # 人工确认分类
    ai_category_id = Column(String(32), nullable=True)                   # AI 分类建议
    ai_category_conf = Column(Float, nullable=True)                      # 分类置信度
    ai_category_reason = Column(Text, nullable=True)                     # 分类理由

    # 检索字段（published 时生成）
    # embedding 字段使用 pgvector，需要在数据库层面定义
    # tsv 字段使用 tsvector，需要在数据库层面定义

    # 状态机字段
    status = Column(String(20), nullable=False, default="draft")         # draft/published/archived/rejected
    reviewer_id = Column(Integer, nullable=True)                         # 审核人 ID
    reviewed_at = Column(DateTime(timezone=True), nullable=True)         # 审核时间
    review_note = Column(Text, nullable=True)                            # 审核备注
    published_at = Column(DateTime(timezone=True), nullable=True)        # 发布时间
    archived_at = Column(DateTime(timezone=True), nullable=True)         # 归档时间

    # 命中统计（case 级去重，物化列）
    hit_count = Column(Integer, nullable=False, default=0)               # 有多少个唯一 case 命中此条目（S4 根因确认时 +1）

    # 时间戳
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )

    # 合法状态集合
    VALID_STATUSES = frozenset({"draft", "published", "archived", "rejected"})

    def __repr__(self) -> str:
        return f"<KbdEntry(id={self.id}, support_id={self.support_id}, status={self.status})>"
