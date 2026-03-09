"""
KB Service SQLAlchemy 模型 — kb_chunk
"""

from datetime import UTC, datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import Column, DateTime, ForeignKey, Integer, SmallInteger, String, Text
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import relationship

from shared.database.postgres import Base


class KBChunk(Base):
    """知识库分块 + 向量模型

    双路检索：
    - embedding（vector_cosine_ops）：语义向量检索
    - tsv（GIN）：BM25 全文检索

    tsv 字段由 ingestor 在写入时使用 jieba 分词后执行：
        func.to_tsvector('simple', ' '.join(jieba.cut(content)))
    """

    __tablename__ = "kb_chunk"

    id = Column(Integer, primary_key=True, autoincrement=True)
    document_id = Column(
        Integer,
        ForeignKey("kb_document.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    chunk_index = Column(SmallInteger, nullable=False)                    # 块序号（0-based）
    content = Column(Text, nullable=False)                                # 块文本（~512 tokens）
    embedding = Column(Vector(384), nullable=True)                        # 384 维向量
    token_count = Column(SmallInteger, nullable=True)                     # token 数
    chunk_meta = Column("metadata", JSONB, nullable=True)                 # 块级元数据（标题层级等），DB列名为 metadata
    tsv = Column(TSVECTOR, nullable=True)                                 # BM25 全文索引
    trace_id = Column(String(64), nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False)

    # 反向关联
    document = relationship("KBDocument", back_populates="chunks")

    def __repr__(self) -> str:
        return f"<KBChunk(id={self.id}, document_id={self.document_id}, idx={self.chunk_index})>"
