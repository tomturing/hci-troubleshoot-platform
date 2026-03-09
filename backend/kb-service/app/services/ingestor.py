"""
文档入库服务 — Ingestor

完整流程：
  1. 接收 IngestRequest（title + content_md + 元数据）
  2. 计算 SHA256 内容哈希，跳过未变更的文档（幂等）
  3. 将文档写入 kb_document（status=published，因为是通过 API 主动入库）
  4. 使用 TextSplitter 分块（512 tokens, 128 overlap）
  5. 批量调用 EmbeddingService 获取向量
  6. 使用 jieba 分词，计算 tsvector
  7. 批量写入 kb_chunk（包含 embedding + tsv）

注意事项：
- 所有 DB 操作在同一事务内完成（原子性）
- 日志记录每步耗时，异常时保留 trace_id 以便追查
- 向量写入使用 pgvector 的 ARRAY 格式（通过 SQLAlchemy + pgvector 扩展）
"""

from __future__ import annotations

import hashlib
import time
from typing import TYPE_CHECKING

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.utils.logger import get_logger
from shared.utils.otel import get_current_trace_id

from app.models.chunk import KBChunk
from app.models.document import KBDocument
from app.utils.jieba_hci import segment
from app.utils.text_splitter import TextSplitter

if TYPE_CHECKING:
    from shared.database.postgres import DatabaseManager

    from app.services.embedding import EmbeddingService

logger = get_logger("kb-service-ingestor")


class IngestResult:
    """入库结果"""

    def __init__(self, document_id: int, chunks_created: int, skipped: bool = False):
        self.document_id = document_id
        self.chunks_created = chunks_created
        self.skipped = skipped          # True 表示文档未变更，跳过入库

    def to_dict(self) -> dict:
        return {
            "document_id": self.document_id,
            "chunks_created": self.chunks_created,
            "skipped": self.skipped,
        }


class IngestorService:
    """文档入库服务"""

    def __init__(self, db_manager: "DatabaseManager", embedding_service: "EmbeddingService"):
        self._db = db_manager
        self._embedding = embedding_service
        self._splitter = TextSplitter(chunk_size=512, chunk_overlap=128)

    async def ingest(
        self,
        *,
        title: str,
        content_md: str,
        source_id: str | None = None,
        source_type: str = "kb",
        category_l1: str | None = None,
        category_l2: str | None = None,
        tags: list[str] | None = None,
        summary: str | None = None,
        judgment_logic: str | None = None,
        yaml_meta: dict | None = None,
        difficulty: int = 3,
        verified_version: str | None = None,
    ) -> IngestResult:
        """将文档入库

        幂等性：通过 content_hash 检测未变更文档，直接返回已有 document_id。

        Args:
            title: 文档标题
            content_md: Markdown 全文
            source_id: 原始案例 ID（可选，用于幂等检测）
            source_type: 来源类型 kb/sop/realtime
            ... 其他元数据字段

        Returns:
            IngestResult
        """
        trace_id = get_current_trace_id()
        t_start = time.monotonic()

        # 1. 计算内容哈希
        content_hash = hashlib.sha256(content_md.encode("utf-8")).hexdigest()

        async with self._db.async_session_factory() as session:
            # 2. 检查是否已存在（幂等）
            existing = await self._find_existing(session, source_id, content_hash)
            if existing:
                logger.info(
                    event="ingest_skipped",
                    message=f"文档未变更，跳过入库: {title[:50]}",
                    document_id=existing.id,
                    content_hash=content_hash,
                    trace_id=trace_id,
                )
                return IngestResult(document_id=existing.id, chunks_created=0, skipped=True)

            # 3. 写入 kb_document
            document = KBDocument(
                source_id=source_id,
                title=title,
                content_md=content_md,
                content_hash=content_hash,
                category_l1=category_l1,
                category_l2=category_l2,
                tags=tags or [],
                summary=summary,
                judgment_logic=judgment_logic,
                yaml_meta=yaml_meta or {},
                difficulty=difficulty,
                status="published",         # API 主动入库直接发布
                source_type=source_type,
                verified_version=verified_version,
                trace_id=trace_id,
            )
            session.add(document)
            await session.flush()   # 获取 document.id（SERIAL）

            logger.info(
                event="document_created",
                document_id=document.id,
                title=title[:50],
                source_type=source_type,
                trace_id=trace_id,
            )

            # 4. 分块
            t_chunk_start = time.monotonic()
            chunks_text = self._splitter.split(content_md)
            logger.info(
                event="document_chunked",
                document_id=document.id,
                chunk_count=len(chunks_text),
                chunk_time_ms=int((time.monotonic() - t_chunk_start) * 1000),
                trace_id=trace_id,
            )

            # 5. 批量 Embedding
            t_embed_start = time.monotonic()
            embeddings = await self._embedding.embed_batch(chunks_text)
            logger.info(
                event="embeddings_generated",
                document_id=document.id,
                count=len(embeddings),
                embed_time_ms=int((time.monotonic() - t_embed_start) * 1000),
                trace_id=trace_id,
            )

            # 6. 批量写入 kb_chunk
            for idx, (chunk_text, embedding) in enumerate(zip(chunks_text, embeddings)):
                # jieba 分词后生成 tsvector（在 DB 端执行 to_tsvector）
                jieba_tokens = segment(chunk_text)
                chunk = KBChunk(
                    document_id=document.id,
                    chunk_index=idx,
                    content=chunk_text,
                    embedding=embedding,
                    token_count=len(chunk_text) // 2,   # 粗略估算
                    chunk_meta={"chunk_index": idx, "total_chunks": len(chunks_text)},
                    trace_id=trace_id,
                )
                session.add(chunk)
                # tsv 字段通过 SQL 函数设置（需在 flush 后用 UPDATE 设置，或直接在 INSERT 时用 text()）
                await session.flush()
                # 使用原生 SQL 更新 tsv（to_tsvector 是 DB 函数，SQLAlchemy 不直接支持）
                await session.execute(
                    func.set_config("search_path", "public", True).select()
                )
                await session.execute(
                    KBChunk.__table__.update()
                    .where(KBChunk.id == chunk.id)
                    .values(tsv=func.to_tsvector("simple", jieba_tokens))
                )

            await session.commit()

            total_ms = int((time.monotonic() - t_start) * 1000)
            logger.info(
                event="ingest_completed",
                document_id=document.id,
                chunks_created=len(chunks_text),
                total_ms=total_ms,
                trace_id=trace_id,
            )

            return IngestResult(document_id=document.id, chunks_created=len(chunks_text))

    async def _find_existing(
        self,
        session: AsyncSession,
        source_id: str | None,
        content_hash: str,
    ) -> KBDocument | None:
        """查找已存在的文档（按 source_id 或 content_hash）"""
        # 优先用 source_id（语义唯一性更强）
        if source_id:
            result = await session.execute(
                select(KBDocument).where(KBDocument.source_id == source_id)
            )
            existing = result.scalar_one_or_none()
            if existing:
                # 检查内容是否有变化（content_hash 不同说明需要重新入库）
                if existing.content_hash == content_hash:
                    return existing
                # 内容已变更，需要重新入库（删除旧记录，级联删除 chunks）
                await session.delete(existing)
                await session.flush()
                return None

        # 按 content_hash 查找（无 source_id 时）
        result = await session.execute(
            select(KBDocument).where(KBDocument.content_hash == content_hash)
        )
        return result.scalar_one_or_none()
