"""
KB Service — 检索路由

POST /api/kb/search
  - 混合检索：SOP 精确匹配优先 → BM25 + Vector + RRF 兜底
  - 调用方：ProductionClaw（通过 Conversation Service）
  - 无需鉴权（Pod 内部调用），仅在 K8s NetworkPolicy 层做隔离

POST /api/kb/sop/match
  - 仅做 SOP 关键字精确匹配，不走向量检索
  - 调用方：Conversation Service（需要快速精确判断是否有 SOP 可用）
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from shared.utils.logger import get_logger

from app.services.search_engine import SearchEngine

if TYPE_CHECKING:
    from shared.database.postgres import DatabaseManager

    from app.services.embedding import EmbeddingService
    from app.services.sop_matcher import SopMatcher

logger = get_logger("kb-service-search")
router = APIRouter(prefix="/api/kb", tags=["search"])

# 由 main.py 的 set_dependencies 注入
_db_manager: "DatabaseManager | None" = None
_embedding_service: "EmbeddingService | None" = None
_sop_matcher: "SopMatcher | None" = None


def set_dependencies(
    db: "DatabaseManager",
    embedding: "EmbeddingService",
    sop: "SopMatcher",
) -> None:
    global _db_manager, _embedding_service, _sop_matcher
    _db_manager = db
    _embedding_service = embedding
    _sop_matcher = sop


# ---- 请求/响应模型 ----

class SearchRequest(BaseModel):
    """混合检索请求"""

    query: str = Field(..., min_length=1, max_length=500, description="用户问题")
    top_n: int = Field(5, ge=1, le=20, description="返回 chunk 数量")
    category_l1: str | None = Field(None, description="一级分类过滤（None=不过滤）")
    category_l2: str | None = Field(None, description="二级分类过滤")
    include_sop: bool = Field(True, description="是否优先尝试 SOP 精确匹配")


class SopMatchRequest(BaseModel):
    """SOP 精确匹配请求"""

    query: str = Field(..., min_length=1, max_length=500)


# ---- 路由 ----

@router.post("/search")
async def search(request: Request, body: SearchRequest):
    """混合知识检索

    流程：
    1. （可选）SOP 关键字精确匹配
    2. BM25 + 向量 → RRF 融合
    3. 返回最多 top_n 个 chunk + 可能的 SOP 命中节点

    响应体：
    ```json
    {
      "sop_match": null | { skill_id, node_name, matched_keyword, content, file_path },
      "chunks": [ { chunk_id, document_id, content, rrf_score, ... } ],
      "query": "..."
    }
    ```
    """
    if _db_manager is None or _embedding_service is None:
        raise HTTPException(status_code=503, detail="服务未就绪")

    logger.info(event="search_request", query=body.query[:50], top_n=body.top_n)

    # 1. SOP 精确匹配
    sop_match = None
    if body.include_sop and _sop_matcher:
        sop_result = _sop_matcher.match(body.query)
        if sop_result:
            sop_match = sop_result.to_dict()

    # 2. 向量 + BM25 混合检索
    engine = SearchEngine(
        db_manager=_db_manager,
        embedding_service=_embedding_service,
    )
    chunks = await engine.search(
        query=body.query,
        top_n=body.top_n,
        category_l1=body.category_l1,
        category_l2=body.category_l2,
    )

    return {
        "query": body.query,
        "sop_match": sop_match,
        "chunks": [c.to_dict() for c in chunks],
        "total": len(chunks),
    }


@router.post("/sop/match")
async def sop_match(request: Request, body: SopMatchRequest):
    """仅执行 SOP 关键字精确匹配（不走向量检索）

    用于 Conversation Service 快速判断是否有 SOP 可用。
    返回匹配到的 SOP 节点，或 { "matched": false }。
    """
    if _sop_matcher is None:
        raise HTTPException(status_code=503, detail="SOP 匹配器未就绪")

    result = _sop_matcher.match(body.query)
    if result:
        return {"matched": True, **result.to_dict()}
    return {"matched": False}
