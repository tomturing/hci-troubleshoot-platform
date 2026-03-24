"""
KB Service — 知识原子搜索服务

双路检索逻辑：
  路由 1（优先）：task_error_keywords → JSONB @> 精确匹配 trigger.task_error_keywords
  路由 2（fallback）：pgvector 余弦相似度语义检索

版本过滤：
  若请求携带 hci_version，则过滤 applicable_version_min/max 范围之外的原子。
  无 hci_version 时跳过版本过滤。

参数传递：
  使用 SQLAlchemy text() 命名参数（:name 格式），避免位置参数索引混乱。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

import sqlalchemy as sa
from shared.database.postgres import DatabaseManager

from app.services.embedding import EmbeddingService

logger = logging.getLogger(__name__)


@dataclass
class AtomResult:
    """知识原子检索结果"""

    id: str
    type: str
    category_id: str | None
    trigger: dict
    content: dict
    confidence: float
    verified: bool
    score: float
    matched_by: str  # "exact" | "vector"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "type": self.type,
            "category_id": self.category_id,
            "trigger": self.trigger,
            "content": self.content,
            "confidence": self.confidence,
            "verified": self.verified,
            "score": self.score,
            "matched_by": self.matched_by,
        }


class AtomSearchService:
    """知识原子双路检索服务"""

    def __init__(
        self,
        db_manager: DatabaseManager,
        embedding_service: EmbeddingService,
    ) -> None:
        self._db = db_manager
        self._emb = embedding_service

    async def search(
        self,
        query: str,
        category_id: str | None = None,
        knowledge_domain: str | None = None,
        stage: str | None = None,
        hci_version: str | None = None,
        top_k: int = 5,
        task_error_keywords: list[str] | None = None,
    ) -> tuple[list[AtomResult], str]:
        """执行双路检索，返回 (results, matched_by)"""
        if task_error_keywords:
            exact_results = await self._exact_keyword_search(
                keywords=task_error_keywords,
                category_id=category_id,
                knowledge_domain=knowledge_domain,
                stage=stage,
                hci_version=hci_version,
                top_k=top_k,
            )
            if exact_results:
                logger.info(
                    "原子精确匹配命中: keywords=%s count=%d",
                    task_error_keywords[:3],
                    len(exact_results),
                )
                return exact_results, "exact"

        semantic_results = await self._semantic_search(
            query=query,
            category_id=category_id,
            knowledge_domain=knowledge_domain,
            stage=stage,
            hci_version=hci_version,
            top_k=top_k,
        )
        logger.info("原子语义检索完成: query=%s count=%d", query[:50], len(semantic_results))
        return semantic_results, "semantic"

    async def _exact_keyword_search(
        self,
        keywords: list[str],
        category_id: str | None,
        knowledge_domain: str | None,
        stage: str | None,
        hci_version: str | None,
        top_k: int,
    ) -> list[AtomResult]:
        """JSONB @> 精确匹配 trigger.task_error_keywords（OR 逻辑）"""
        kw_conditions = " OR ".join(
            f"(trigger->'task_error_keywords') @> :kw_{i}::jsonb"
            for i in range(len(keywords))
        )
        params: dict = {f"kw_{i}": json.dumps(kw, ensure_ascii=False) for i, kw in enumerate(keywords)}
        where_clauses = [f"({kw_conditions})"]
        self._append_filters(params, where_clauses, category_id, knowledge_domain, stage, hci_version)
        params["top_k"] = top_k

        where_sql = " AND ".join(where_clauses)
        sql = sa.text(f"""
        SELECT id, type, category_id, trigger, content,
               confidence, verified, 1.0::float AS score
        FROM knowledge_atoms
        WHERE {where_sql}
        ORDER BY verified DESC, confidence DESC
        LIMIT :top_k
        """)

        async with self._db.async_session_factory() as session:
            result = await session.execute(sql, params)
            rows = result.fetchall()

        return [
            AtomResult(
                id=row[0], type=row[1], category_id=row[2],
                trigger=row[3] or {}, content=row[4] or {},
                confidence=float(row[5] or 0.8), verified=bool(row[6]),
                score=float(row[7]), matched_by="exact",
            )
            for row in rows
        ]

    async def _semantic_search(
        self,
        query: str,
        category_id: str | None,
        knowledge_domain: str | None,
        stage: str | None,
        hci_version: str | None,
        top_k: int,
    ) -> list[AtomResult]:
        """pgvector 余弦距离语义检索（向量化失败时降级为空结果）"""
        try:
            embedding = await self._emb.embed(query)
        except Exception as exc:
            logger.warning("向量化失败，降级至空结果: %s", exc)
            return []

        # 向量字面量注入（不走参数化，避免超长参数限制）
        embedding_literal = "[" + ",".join(f"{v:.8f}" for v in embedding) + "]"
        where_clauses = ["embedding IS NOT NULL"]
        params: dict = {}
        self._append_filters(params, where_clauses, category_id, knowledge_domain, stage, hci_version)
        params["top_k"] = top_k

        where_sql = " AND ".join(where_clauses)
        sql = sa.text(f"""
        SELECT id, type, category_id, trigger, content,
               confidence, verified,
               (1 - (embedding <=> '{embedding_literal}'::vector))::float AS score
        FROM knowledge_atoms
        WHERE {where_sql}
        ORDER BY embedding <=> '{embedding_literal}'::vector
        LIMIT :top_k
        """)

        async with self._db.async_session_factory() as session:
            result = await session.execute(sql, params)
            rows = result.fetchall()

        return [
            AtomResult(
                id=row[0], type=row[1], category_id=row[2],
                trigger=row[3] or {}, content=row[4] or {},
                confidence=float(row[5] or 0.8), verified=bool(row[6]),
                score=float(row[7]), matched_by="vector",
            )
            for row in rows
        ]

    @staticmethod
    def _append_filters(
        params: dict,
        where_clauses: list[str],
        category_id: str | None,
        knowledge_domain: str | None,
        stage: str | None,
        hci_version: str | None,
    ) -> None:
        """将附加过滤条件写入 params 和 where_clauses（原地修改）"""
        if category_id:
            where_clauses.append("category_id = :category_id")
            params["category_id"] = category_id
        if knowledge_domain:
            where_clauses.append("knowledge_domain = :knowledge_domain")
            params["knowledge_domain"] = knowledge_domain
        if stage:
            where_clauses.append("trigger->>'stage' = :stage")
            params["stage"] = stage
        if hci_version:
            # 版本范围宽松过滤：无版本字段的原子视为全版本可用
            where_clauses.append(
                "(applicable_version_min IS NULL OR applicable_version_min <= :hci_version_min)"
            )
            params["hci_version_min"] = hci_version
            where_clauses.append(
                "(applicable_version_max IS NULL OR applicable_version_max >= :hci_version_max)"
            )
            params["hci_version_max"] = hci_version
