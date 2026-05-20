"""
搜索引擎 — BM25 + Vector + RRF 混合检索

检索流程：
  1. SOP 精确匹配（由调用方提前执行，本服务只负责向量/BM25）
  2. BM25 检索：jieba 分词 → to_tsquery → tsvector 全文检索（召回 top-20）
  3. 向量检索：embed(query) → pgvector <=> 余弦相似度（召回 top-20）
  4. RRF 融合：Reciprocal Rank Fusion 合并两路结果（k=60）
  5. 返回 top-N chunks（默认 N=5）

注意事项：
- BM25 和向量检索并发执行（asyncio.gather），减少延迟
- IVFFlat 索引在生产数据 < 1000 条时可能不存在，此时走全表扫描（slower），可接受
- 分类过滤（category_l1/l2）加在 WHERE 子句，减少全表扫描范围
- 所有结果带 trace_id，便于问题追查
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from typing import TYPE_CHECKING

from shared.observability.logger import get_logger
from shared.observability.metrics import KB_SEARCH_DURATION_SECONDS
from shared.observability.otel import get_current_trace_id
from sqlalchemy import func, select

from app.models.chunk import KBChunk
from app.models.document import KBDocument
from app.utils.jieba_hci import segment

if TYPE_CHECKING:
    from shared.database.postgres import DatabaseManager

    from app.services.embedding import EmbeddingService

logger = get_logger("kb-service-search")


class ChunkResult:
    """检索到的 chunk 结果"""

    def __init__(
        self,
        chunk_id: int,
        document_id: int,
        chunk_index: int,
        content: str,
        rrf_score: float,
        document_title: str = "",
        category_l1: str = "",
        category_l2: str = "",
    ):
        self.chunk_id = chunk_id
        self.document_id = document_id
        self.chunk_index = chunk_index
        self.content = content
        self.rrf_score = rrf_score
        self.document_title = document_title
        self.category_l1 = category_l1
        self.category_l2 = category_l2

    def to_dict(self) -> dict:
        return {
            "chunk_id": self.chunk_id,
            "document_id": self.document_id,
            "chunk_index": self.chunk_index,
            "content": self.content,
            "rrf_score": round(self.rrf_score, 4),
            "document_title": self.document_title,
            "category_l1": self.category_l1,
            "category_l2": self.category_l2,
        }


class SearchEngine:
    """混合检索引擎（BM25 + Vector + RRF）"""

    def __init__(
        self,
        db_manager: DatabaseManager,
        embedding_service: EmbeddingService,
        bm25_top_k: int = 20,
        vector_top_k: int = 20,
        rrf_k: int = 60,
    ):
        self._db = db_manager
        self._embedding = embedding_service
        self._bm25_top_k = bm25_top_k
        self._vector_top_k = vector_top_k
        self._rrf_k = rrf_k

    async def search(
        self,
        query: str,
        top_n: int = 5,
        category_l1: str | None = None,
        category_l2: str | None = None,
    ) -> list[ChunkResult]:
        """执行混合检索

        Args:
            query: 用户问题
            top_n: 最终返回的 chunk 数量
            category_l1: 一级分类过滤（None=不过滤）
            category_l2: 二级分类过滤（None=不过滤）

        Returns:
            按 RRF 分数排序的 ChunkResult 列表
        """
        trace_id = get_current_trace_id()
        t_start = time.monotonic()

        # 并发执行 BM25 和向量检索
        bm25_task = asyncio.create_task(
            self._bm25_search(query, category_l1=category_l1, category_l2=category_l2)
        )
        vector_task = asyncio.create_task(
            self._vector_search(query, category_l1=category_l1, category_l2=category_l2)
        )

        bm25_results, vector_results = await asyncio.gather(bm25_task, vector_task, return_exceptions=True)

        # 容错处理：任一路失败不影响另一路
        if isinstance(bm25_results, Exception):
            logger.warning(event="bm25_failed", error=str(bm25_results), trace_id=trace_id)
            bm25_results = []
        if isinstance(vector_results, Exception):
            logger.warning(event="vector_failed", error=str(vector_results), trace_id=trace_id)
            vector_results = []

        # RRF 融合
        ranked = self._rrf_fusion(bm25_results, vector_results, top_n=top_n)

        total_ms = int((time.monotonic() - t_start) * 1000)
        # 上报 KB 检索耗时指标（修复 #6：KB_SEARCH_DURATION_SECONDS 指标之前已定义但从未上报）
        KB_SEARCH_DURATION_SECONDS.observe(total_ms / 1000)
        logger.info(
            event="search_completed",
            query=query[:50],
            bm25_count=len(bm25_results),
            vector_count=len(vector_results),
            final_count=len(ranked),
            total_ms=total_ms,
            trace_id=trace_id,
        )

        return ranked

    async def _bm25_search(
        self,
        query: str,
        category_l1: str | None = None,
        category_l2: str | None = None,
    ) -> list[tuple[int, str, int, str, str, str]]:
        """BM25 全文检索

        Returns list of (chunk_id, content, document_id, document_title, cat_l1, cat_l2)
        """
        # jieba 分词后构建 tsquery（AND 模式）
        tokens = segment(query).split()
        if not tokens:
            return []
        # PostgreSQL tsquery: token1 & token2 & ...
        tsquery_str = " & ".join(tokens[:10])   # 限制 10 个词，避免过长

        async with self._db.async_session_factory() as session:
            # 构建 SQL：tsvector @@ tsquery，按 ts_rank 排序
            base_query = (
                select(
                    KBChunk.id,
                    KBChunk.content,
                    KBChunk.document_id,
                    KBChunk.chunk_index,
                    KBDocument.title,
                    KBDocument.category_l1,
                    KBDocument.category_l2,
                )
                .join(KBDocument, KBChunk.document_id == KBDocument.id)
                .where(
                    KBChunk.tsv.op("@@")(func.to_tsquery("simple", tsquery_str)),
                    KBDocument.status == "published",
                )
                .order_by(func.ts_rank(KBChunk.tsv, func.to_tsquery("simple", tsquery_str)).desc())
                .limit(self._bm25_top_k)
            )

            if category_l1:
                base_query = base_query.where(KBDocument.category_l1 == category_l1)
            if category_l2:
                base_query = base_query.where(KBDocument.category_l2 == category_l2)

            result = await session.execute(base_query)
            return result.fetchall()

    async def _vector_search(
        self,
        query: str,
        category_l1: str | None = None,
        category_l2: str | None = None,
    ) -> list[tuple[int, str, int, str, str, str]]:
        """向量语义检索

        Returns list of (chunk_id, content, document_id, title, cat_l1, cat_l2)
        """
        # 生成 query 向量
        query_embedding = await self._embedding.embed_single(query)

        async with self._db.async_session_factory() as session:
            base_query = (
                select(
                    KBChunk.id,
                    KBChunk.content,
                    KBChunk.document_id,
                    KBChunk.chunk_index,
                    KBDocument.title,
                    KBDocument.category_l1,
                    KBDocument.category_l2,
                )
                .join(KBDocument, KBChunk.document_id == KBDocument.id)
                .where(
                    KBChunk.embedding.isnot(None),
                    KBDocument.status == "published",
                )
                # pgvector 余弦距离（<=> 越小越相似）
                .order_by(KBChunk.embedding.op("<=>")(query_embedding))
                .limit(self._vector_top_k)
            )

            if category_l1:
                base_query = base_query.where(KBDocument.category_l1 == category_l1)
            if category_l2:
                base_query = base_query.where(KBDocument.category_l2 == category_l2)

            result = await session.execute(base_query)
            return result.fetchall()

    def _rrf_fusion(
        self,
        bm25_results: list,
        vector_results: list,
        top_n: int,
    ) -> list[ChunkResult]:
        """RRF 融合两路检索结果

        公式: score(d) = Σ 1/(k + rank_i(d))，其中 k=60（经验值）

        同一 chunk_id 在两路结果中均出现时，分数叠加，自然提升排名。
        """
        scores: dict[int, float] = defaultdict(float)
        metadata: dict[int, tuple] = {}   # chunk_id → (content, document_id, chunk_index, title, cat_l1, cat_l2)

        # BM25 贡献分
        for rank, row in enumerate(bm25_results):
            chunk_id = row[0]
            scores[chunk_id] += 1.0 / (self._rrf_k + rank + 1)
            if chunk_id not in metadata:
                metadata[chunk_id] = row

        # 向量检索贡献分
        for rank, row in enumerate(vector_results):
            chunk_id = row[0]
            scores[chunk_id] += 1.0 / (self._rrf_k + rank + 1)
            if chunk_id not in metadata:
                metadata[chunk_id] = row

        # 排序并截取 top_n
        ranked_ids = sorted(scores.keys(), key=lambda cid: scores[cid], reverse=True)[:top_n]

        results = []
        for chunk_id in ranked_ids:
            row = metadata[chunk_id]
            results.append(
                ChunkResult(
                    chunk_id=row[0],
                    content=row[1],
                    document_id=row[2],
                    chunk_index=row[3],
                    document_title=row[4] or "",
                    category_l1=row[5] or "",
                    category_l2=row[6] or "",
                    rrf_score=scores[chunk_id],
                )
            )

        return results
