"""
知识库数据库模型 (KB RAG)

用于存储知识库文档和向量分块，支持语义检索。
- KBDocument: 知识库文档表
- KBChunk: 文档分块 + 向量表
"""

import uuid

from pgvector.sqlalchemy import Vector
from sqlalchemy import JSON, Column, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import UUID

from ..database.postgres import Base
from .base import TimestampMixin, TraceableMixin


class KBDocument(Base, TimestampMixin, TraceableMixin):
    """知识库文档模型

    存储原始排障文档，支持 Markdown、SOP、网页案例等多种类型。
    """

    __tablename__ = "kb_document"

    doc_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    title = Column(String(500), nullable=False)
    category = Column(String(100), index=True)
    doc_type = Column(String(50), nullable=False, default="markdown", index=True)  # markdown / sop / web_case
    source_path = Column(Text)  # 原始文件路径或 URL
    content = Column(Text, nullable=False)  # 原始全文
    metadata_ = Column("metadata", JSON, default=dict)  # 扩展元数据（使用 callable 避免可变默认值问题）

    def __repr__(self):
        return f"<KBDocument(doc_id={self.doc_id}, title={self.title[:30]}...)>"


class KBChunk(Base, TraceableMixin):
    """知识库分块 + 向量模型

    存储文档分块及其向量嵌入，用于 RAG 语义检索。
    使用 bge-small-zh-v1.5 模型，输出 384 维向量。
    """

    __tablename__ = "kb_chunk"

    chunk_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    doc_id = Column(
        UUID(as_uuid=True),
        ForeignKey("kb_document.doc_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    chunk_index = Column(Integer, nullable=False)  # 在文档中的位置
    content = Column(Text, nullable=False)  # 分块文本 (~512 tokens)
    embedding = Column(Vector(384))  # bge-small-zh-v1.5 输出维度 384
    metadata_ = Column("metadata", JSON, default=dict)  # 扩展元数据（使用 callable 避免可变默认值问题）
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),  # 数据库端默认值，支持批量插入
    )

    def __repr__(self):
        return f"<KBChunk(chunk_id={self.chunk_id}, doc_id={self.doc_id}, index={self.chunk_index})>"
