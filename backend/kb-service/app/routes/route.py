"""
KB Service — 三轨路由 API

GET /api/kb/route
  - 三轨串行路由：SOP 优先 → KBD 覆盖 → 人工兜底
  - 调用方：Conversation Service（意图识别后调用）
  - 无需鉴权（Pod 内部调用）

三轨优先级：
  1. SOP：标准操作流程（sop_document 表尚未创建，暂时跳过）
  2. KBD：知识库条目（已发布的 kbd_entry）
  3. 人工兜底：无匹配结果时返回 human_escalation
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field
from shared.utils.logger import get_logger

if TYPE_CHECKING:
    from shared.database.postgres import DatabaseManager

logger = get_logger("kb-service-route")
router = APIRouter(prefix="/api/kb", tags=["route"])

# 由 main.py 的 set_dependencies 注入
_db_manager: DatabaseManager | None = None


def set_dependencies(db: DatabaseManager) -> None:
    """注入数据库依赖"""
    global _db_manager
    _db_manager = db


# ---- 响应模型 ----


class RouteResult(BaseModel):
    """单条 KBD 检索结果"""

    id: int = Field(..., description="KBD 条目 ID")
    title: str = Field(..., description="条目标题")
    content_md: str | None = Field(None, description="Markdown 内容")
    support_id: str = Field(..., description="深信服案例 ID")
    category_id: str | None = Field(None, description="分类编码")


class RouteResponse(BaseModel):
    """三轨路由响应"""

    track: str = Field(..., description="路由轨道: sop | kbd | human_escalation")
    category_id: str = Field(..., description="请求的分类 ID")
    results: list[RouteResult] = Field(default_factory=list, description="检索结果")


# ---- 路由 ----


@router.get("/route", response_model=RouteResponse)
async def route(
    request: Request,
    category_id: str = Query(..., min_length=1, description="分类 ID（如 虚拟机-001）"),
    query: str = Query(..., min_length=1, max_length=500, description="用户问题"),
    top_k: int = Query(5, ge=1, le=20, description="返回条目数量"),
):
    """三轨串行路由

    流程：
    1. SOP 轨：标准操作流程（sop_document 表尚未创建，暂时跳过）
    2. KBD 轨：知识库条目检索（BM25 全文检索）
    3. 人工轨：无匹配结果时返回 human_escalation

    响应体：
    ```json
    {
      "track": "kbd",
      "category_id": "虚拟机-001",
      "results": [
        {
          "id": 123,
          "title": "...",
          "content_md": "...",
          "support_id": "36156",
          "category_id": "虚拟机-001"
        }
      ]
    }
    ```
    """
    if _db_manager is None:
        raise HTTPException(status_code=503, detail="服务未就绪")

    logger.info(
        event="route_request",
        category_id=category_id,
        query=query[:50],
        top_k=top_k,
    )

    # 第 1 轨：SOP 优先（sop_document 表尚未创建，暂时跳过）
    # TODO: sop_document 表创建后启用
    # sop_docs = await _db_manager.fetch(
    #     """
    #     SELECT id, title, content
    #     FROM sop_document
    #     WHERE category_id = $1 AND status = 'published'
    #     ORDER BY updated_at DESC
    #     LIMIT $2
    #     """,
    #     category_id,
    #     top_k,
    # )
    # if sop_docs:
    #     logger.info(event="route_sop_matched", category_id=category_id, count=len(sop_docs))
    #     return RouteResponse(
    #         track="sop",
    #         category_id=category_id,
    #         results=[
    #             RouteResult(
    #                 id=doc["id"],
    #                 title=doc["title"],
    #                 content_md=doc.get("content"),
    #                 support_id=f"sop-{doc['id']}",
    #                 category_id=category_id,
    #             )
    #             for doc in sop_docs
    #         ],
    #     )

    # 第 2 轨：KBD 覆盖
    # 使用 PostgreSQL 全文检索（tsvector + ts_rank）
    kbd_entries = await _db_manager.fetch(
        """
        SELECT id, title, content_md, support_id, category_id
        FROM kbd_entry
        WHERE category_id = $1 AND status = 'published'
        ORDER BY ts_rank(tsv, plainto_tsquery('simple', $2)) DESC
        LIMIT $3
        """,
        category_id,
        query,
        top_k,
    )

    if kbd_entries:
        logger.info(
            event="route_kbd_matched",
            category_id=category_id,
            count=len(kbd_entries),
            query=query[:50],
        )
        return RouteResponse(
            track="kbd",
            category_id=category_id,
            results=[
                RouteResult(
                    id=entry["id"],
                    title=entry["title"],
                    content_md=entry.get("content_md"),
                    support_id=entry["support_id"],
                    category_id=entry.get("category_id"),
                )
                for entry in kbd_entries
            ],
        )

    # 第 3 轨：人工兜底
    logger.info(
        event="route_human_escalation",
        category_id=category_id,
        query=query[:50],
        reason="no_kbd_match",
    )
    return RouteResponse(
        track="human_escalation",
        category_id=category_id,
        results=[],
    )
