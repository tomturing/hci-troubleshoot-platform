"""
KB Service SQLAlchemy 模型 — sop_chunk

对应数据库表：sop_chunk（SOP 分块检索表）
按 SOP 章节拆分，支持向量检索和全文检索
"""

from datetime import UTC, datetime

from pgvector.sqlalchemy import Vector
from shared.database.postgres import Base
from sqlalchemy import Column, DateTime, ForeignKey, Integer, SmallInteger, String, Text
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import relationship


class SopChunk(Base):
    """SOP 分块检索模型

    按 SOP 章节拆分，支持：
    1. 向量语义检索（embedding）
    2. 全文检索（tsv）

    embedding 和 tsv 在审核发布时生成，入库时暂不生成。
    """

    __tablename__ = "sop_chunk"

    id = Column(Integer, primary_key=True, autoincrement=True)
    document_id = Column(
        Integer,
        ForeignKey("sop_document.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    chunk_index = Column(SmallInteger, nullable=False)                    # 分块序号（0-based）
    chapter_title = Column(String(200), nullable=True)                    # 章节标题（如"问题诊断"）
    content = Column(Text, nullable=False)                                # 分块内容
    embedding = Column(Vector(1536), nullable=True)                       # 语义向量（1536 维），审核时生成
    tsv = Column(TSVECTOR, nullable=True)                                 # 全文检索向量，审核时生成
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False)

    # 反向关联
    document = relationship("SopDocument", back_populates="chunks")

    def __repr__(self) -> str:
        return f"<SopChunk(id={self.id}, document_id={self.document_id}, idx={self.chunk_index}, chapter={self.chapter_title})>"
