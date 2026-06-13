"""
KB Service — KBD 语义检索路由（供 agent-service 使用）

GET /api/kb/kbd/search
  - 调用方：agent-service 的 InvestigationAgent（通过 KBClient.search_cases_with_steps）
  - 仅返回 status='published' AND steps_json != '[]' 的 KBD 条目
  - 混合排序：向量语义相似度（有 embedding） + 命中次数加权
  - 无需鉴权（Pod 内部调用），仅在 K8s NetworkPolicy 层做隔离

返回格式与 agent-service 中 kbd_from_dict() 期望的字段一致：
  - "name" 字段映射到 KBD.name（= title）
  - "steps" 字段来自 steps_json 列（[{tool_name, tool_args_template, expected_pattern}]）
  - "similarity" 为向量余弦相似度（无 embedding 时降级为 0.0）
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Query, Request
from shared.dynamic_resource.adapters import kbd_resource_payload
from shared.dynamic_resource.loader import DynamicResourceLoader, snapshot_revision_metadata
from shared.dynamic_resource.models import UsageRecord
from shared.dynamic_resource.publisher import DynamicResourcePublisher
from shared.observability.logger import get_logger
from shared.observability.otel import get_current_trace_id
from sqlalchemy import and_, select, text

from app.models.kbd_entry import KbdEntry

if TYPE_CHECKING:
    from shared.database.postgres import DatabaseManager

    from app.services.embedding import EmbeddingService

logger = get_logger("kb-service-kbd-search")
router = APIRouter(prefix="/api/kb", tags=["kbd"])

# 由 main.py 的 set_dependencies 注入
_db_manager: DatabaseManager | None = None
_embedding_service: EmbeddingService | None = None


def set_dependencies(db: DatabaseManager, embedding: EmbeddingService) -> None:
    """由 main.py lifespan 调用，注入数据库和 embedding 服务依赖"""
    global _db_manager, _embedding_service
    _db_manager = db
    _embedding_service = embedding


# ── 响应模型 ──────────────────────────────────────────────────────────────────


def _entry_to_case_dict(entry: KbdEntry, similarity: float) -> dict[str, Any]:
    """将 KbdEntry ORM 对象转换为 cases/search 响应格式。

    字段名与 agent-service kbd_from_dict() 期望保持一致：
    - "name" = entry.title（KBD.name）
    - "steps" = entry.steps_json（[{tool_name, tool_args_template, expected_pattern}]）
    - "similarity" = 向量余弦相似度（范围 [0.0, 1.0]）
    """
    return {
        "id": str(entry.id),
        "name": entry.title,  # kbd_from_dict 期望 "name"
        "category_id": entry.category_id or "",
        "similarity": round(similarity, 4),
        # 8 大章节字段（叙述类，供 LLM 上下文注入）
        "problem_description": entry.problem_description,
        "alert_info": entry.alert_info,
        "steps_text": entry.steps_text,
        "root_cause": entry.root_cause,
        "solution": entry.solution,
        "operational_impact": entry.operational_impact,
        "is_temporary": entry.is_temporary,
        "recommendations": entry.recommendations,
        # 结构化工具步骤（供 InvestigationAgent 执行）
        "steps": entry.steps_json,  # kbd_from_dict 期望 "steps"
    }


async def _entry_to_case_with_revision(
    session: Any,
    entry: KbdEntry,
    similarity: float,
    *,
    category_id: str,
    query: str,
    top_k: int,
    used_vector: bool,
) -> dict[str, Any]:
    """返回 KBD 候选并记录其动态资源 revision 使用审计。"""
    trace_id = get_current_trace_id()
    snapshot = await DynamicResourcePublisher(session).ensure_published(**kbd_resource_payload(entry), trace_id=trace_id)
    await DynamicResourceLoader(session).audit_usage(
        snapshot,
        UsageRecord(
            consumer="kb-service.kbd_search",
            status="matched",
            trace_id=trace_id,
            input_payload={"category_id": category_id, "query": query, "top_k": top_k},
            output_payload={"similarity": round(similarity, 4)},
            metadata={"used_vector": used_vector},
        ),
    )
    case = _entry_to_case_dict(entry, similarity)
    case["resource_revision"] = snapshot_revision_metadata(snapshot)
    return case


# ── 路由 ──────────────────────────────────────────────────────────────────────


@router.get("/kbd/search")
async def search_kbds(
    request: Request,
    category_id: str = Query(..., min_length=1, max_length=32, description="分类 ID（精确匹配 kb_category.code）"),
    query: str = Query(..., min_length=1, max_length=500, description="用户问题文本，用于向量语义排序"),
    top_k: int = Query(15, ge=1, le=50, description="返回最多 top_k 个条目"),
) -> dict[str, list[dict[str, Any]]]:
    """KBD 语义检索（agent-service 专用）

    仅返回满足以下条件的 KBD 条目：
    1. status = 'published'（已审核发布）
    2. steps_json != '[]'（有结构化工具步骤，InvestigationAgent 可执行）
    3. category_id 精确匹配

    排序策略：
    - 有 embedding：按向量余弦相似度降序（embedding <=> query_vector ASC）
    - 无 embedding（降级）：按 published_at DESC

    返回格式（与 KBClient.search_cases_with_steps 期望一致）：
    ```json
    {
      "cases": [
        {
          "id": "123",
          "name": "...",
          "category_id": "虚拟机-003",
          "similarity": 0.87,
          "problem_description": "...",
          "alert_info": "...",
          "steps_text": "...",
          "root_cause": "...",
          "solution": "...",
          "operational_impact": "...",
          "is_temporary": "...",
          "recommendations": "...",
          "steps": [{"tool_name": "...", "tool_args_template": {}, "expected_pattern": "..."}]
        }
      ]
    }
    ```
    """
    if _db_manager is None or _embedding_service is None:
        raise HTTPException(status_code=503, detail="服务依赖未初始化")

    logger.info(
        event="kbd_search_start",
        category_id=category_id,
        query_len=len(query),
        top_k=top_k,
    )

    # 1. 生成查询向量（失败时降级到非向量排序）
    query_vector: list[float] | None = None
    try:
        query_vector = await _embedding_service.embed_single(query)
    except Exception as e:
        logger.warning(
            event="kbd_search_embedding_failed",
            error=str(e),
            fallback="published_at_desc",
        )

    async with _db_manager.async_session_factory() as session:
        if query_vector is not None:
            # 向量语义排序（pgvector cosine distance，ASC = 越小越相似）
            # 同时过滤 status + steps_json + category_id
            vector_str = f"[{','.join(str(v) for v in query_vector)}]"
            stmt = (
                select(
                    KbdEntry,
                    text(f"1 - (embedding <=> '{vector_str}'::vector) AS similarity"),
                )
                .where(
                    and_(
                        KbdEntry.status == "published",
                        KbdEntry.category_id == category_id,
                        text("steps_json != '[]'::jsonb"),
                        text("embedding IS NOT NULL"),
                    )
                )
                .order_by(text(f"embedding <=> '{vector_str}'::vector"))
                .limit(top_k)
            )
            results = await session.execute(stmt)
            rows = results.all()
            cases = [
                await _entry_to_case_with_revision(
                    session,
                    row[0],
                    float(row[1]),
                    category_id=category_id,
                    query=query,
                    top_k=top_k,
                    used_vector=True,
                )
                for row in rows
            ]
        else:
            # 降级：无向量时按 published_at DESC
            stmt_fallback = (
                select(KbdEntry)
                .where(
                    and_(
                        KbdEntry.status == "published",
                        KbdEntry.category_id == category_id,
                        text("steps_json != '[]'::jsonb"),
                    )
                )
                .order_by(KbdEntry.published_at.desc())
                .limit(top_k)
            )
            results_fallback = await session.execute(stmt_fallback)
            entries = results_fallback.scalars().all()
            cases = [
                await _entry_to_case_with_revision(
                    session,
                    entry,
                    0.0,
                    category_id=category_id,
                    query=query,
                    top_k=top_k,
                    used_vector=False,
                )
                for entry in entries
            ]

        await session.commit()

    logger.info(
        event="kbd_search_done",
        category_id=category_id,
        result_count=len(cases),
        used_vector=query_vector is not None,
    )

    return {"cases": cases}
