"""
KB Service — KBD 候选检索路由（供 agent-service 使用）。

GET /api/kb/kbd/search
  - 仅检索已发布、分类匹配且包含可执行关键信号的 KBD
  - 向量候选必须通过最低相似度门禁
  - 向量不可用或无合格候选时，降级到 jieba + PostgreSQL FTS
  - 两条路径均无结果时返回空列表，不按时间制造无关候选
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field
from shared.dynamic_resource.loader import DynamicResourceLoader, snapshot_revision_metadata
from shared.dynamic_resource.models import UsageRecord, UsageStatus
from shared.observability.logger import get_logger
from shared.observability.otel import get_current_trace_id
from sqlalchemy import case as sql_case
from sqlalchemy import select, text

from app.config import settings
from app.models.kbd_entry import KbdEntry
from app.utils.jieba_hci import segment

if TYPE_CHECKING:
    from shared.database.postgres import DatabaseManager

    from app.services.embedding import EmbeddingService

logger = get_logger("kb-service-kbd-search")
router = APIRouter(prefix="/api/kb", tags=["kbd"])

_db_manager: DatabaseManager | None = None
_embedding_service: EmbeddingService | None = None

_USABLE_SIGNALS_SQL = """
(
    (jsonb_typeof(signals_json) = 'array' AND jsonb_array_length(signals_json) > 0)
    OR
    (
        jsonb_typeof(signals_json) = 'object'
        AND jsonb_typeof(signals_json->'signals') = 'array'
        AND jsonb_array_length(signals_json->'signals') > 0
    )
)
"""


def set_dependencies(db: DatabaseManager, embedding: EmbeddingService) -> None:
    """由 main.py lifespan 注入数据库和 embedding 服务。"""
    global _db_manager, _embedding_service
    _db_manager = db
    _embedding_service = embedding


def _entry_to_case_dict(entry: KbdEntry, similarity: float) -> dict[str, Any]:
    """将 KbdEntry 转换为 agent-service 消费格式。"""
    raw_signals = entry.signals_json
    signals_list = raw_signals.get("signals", []) if isinstance(raw_signals, dict) else raw_signals
    return {
        "id": str(entry.id),
        "name": entry.title,
        "category_id": entry.category_id or "",
        "similarity": round(similarity, 4),
        "problem_description": entry.problem_description,
        "alert_info": entry.alert_info,
        "steps_text": entry.steps_text,
        "root_cause": entry.root_cause,
        "solution": entry.solution,
        "operational_impact": entry.operational_impact,
        "is_temporary": entry.is_temporary,
        "recommendations": entry.recommendations,
        "signals": signals_list,
    }


class SemanticCaseContext(BaseModel):
    """客户已提交事实的检索载体；字段均不得由 LLM 推断。"""

    description: str = Field(min_length=1, max_length=16000)
    error_text: str | None = Field(default=None, max_length=1000)
    object_type: str | None = Field(default=None, max_length=120)
    operation: str | None = Field(default=None, max_length=120)
    product: str | None = Field(default=None, max_length=120)
    product_version: str | None = Field(default=None, max_length=120)
    component: str | None = Field(default=None, max_length=120)


class SemanticEntryResolveRequest(BaseModel):
    """受限语义入口请求；context 仅允许用户已提交的故障文字。"""

    category_id: str = Field(min_length=1, max_length=32)
    # 兼容历史调用方传字符串；新调用方传结构化片段，KB 服务仍会做确定性归一化。
    case_context: SemanticCaseContext | Annotated[str, Field(min_length=1, max_length=16000)] = Field(
        union_mode="left_to_right"
    )
    strong_producer_status: Literal["matched", "no_match", "source_unavailable", "not_applicable"] = Field(
        description="真实未命中为 no_match；分类没有强生产者为 not_applicable；已命中或查询不可用时禁止兜底"
    )
    top_k: int = Field(default=5, ge=1, le=10)
    expected_revisions: dict[str, int] = Field(default_factory=dict)


@router.post("/kbd/semantic-entry/resolve")
async def resolve_semantic_entry(request: SemanticEntryResolveRequest) -> dict[str, Any]:
    """只从权威发布快照检索，返回可审计候选而非根因。"""
    from shared.dynamic_resource.loader import ResourceNotFoundError
    from shared.observability.redaction import redact_observation_value
    from shared.schemas.semantic_routing import resolve_candidates

    from app.routes.playbooks import _execution_issues

    if request.strong_producer_status not in {"no_match", "not_applicable"}:
        return await resolve_candidates(
            entries=[],
            context=request.case_context.model_dump()
            if isinstance(request.case_context, SemanticCaseContext)
            else request.case_context,
            strong_status=request.strong_producer_status,
        )
    if _db_manager is None:
        raise HTTPException(status_code=503, detail="服务依赖未初始化")
    context = (
        request.case_context.model_dump(exclude_none=True)
        if isinstance(request.case_context, SemanticCaseContext)
        else request.case_context
    )
    context = redact_observation_value(context)
    entries = []
    async with _db_manager.async_session_factory() as session:
        loader = DynamicResourceLoader(session)
        result = await session.execute(
            select(KbdEntry).where(KbdEntry.category_id == request.category_id, KbdEntry.status == "published")
        )
        for entry in result.scalars().all():
            try:
                snapshot = await loader.get_active("kbd", str(entry.id))
            except ResourceNotFoundError:
                continue
            content = snapshot.content
            if snapshot.status != "published" or content.get("status", "published") != "published":
                continue
            if content.get("category_id") != request.category_id:
                continue
            expected = request.expected_revisions.get(str(entry.id))
            if expected is not None and expected != snapshot.revision:
                raise HTTPException(status_code=409, detail="KBD 画像修订已变更，请重新加载分类快照后重试")
            document = content.get("signals_json") or {}
            if not isinstance(document, dict):
                document = {"schema_version": 2, "signals": document}
            issues = _execution_issues(
                document.get("signals") or [],
                document,
                support_id=content.get("support_id"),
                category_id=request.category_id,
            )
            entries.append(
                {
                    **content,
                    "id": str(entry.id),
                    "signals_json": document,
                    "executable": not issues,
                    "resource_revision": snapshot_revision_metadata(snapshot),
                }
            )
        if request.expected_revisions and {entry["id"] for entry in entries} != set(request.expected_revisions):
            raise HTTPException(status_code=409, detail="分类发布成员已变更，请重新加载完整分类快照后重试")
        resolved = await resolve_candidates(
            entries=entries,
            context=context,
            strong_status=request.strong_producer_status,
            embed=_embedding_service.embed_batch if _embedding_service else None,
            embedding_namespace=(str(settings.LLM_BASE_URL) + ":" + _embedding_service.model_name)
            if _embedding_service
            else "",
            top_k=request.top_k,
        )
        # 运行时使用的精确修订进入现有审计，停用／回滚不会归属到错误版本。
        for candidate in resolved["candidates"]:
            revision = candidate.get("resource_revision") or {}
            snapshot = await loader.get_revision("kbd", candidate["kbd_id"], int(revision["revision"]))
            await loader.audit_usage(
                snapshot,
                UsageRecord(
                    consumer="semantic-entry",
                    trace_id=get_current_trace_id(),
                    status=UsageStatus.SUCCESS,
                    input_payload=context,
                    output_payload=candidate,
                    metadata={
                        "routing_version": resolved["routing_version"],
                        "profile_digest": candidate["profile_digest"],
                    },
                ),
            )
        await session.commit()
    return resolved


async def _load_entries_in_order(session: Any, ids: list[int]) -> list[KbdEntry]:
    """按粗排 ID 顺序加载 ORM 对象。"""
    if not ids:
        return []
    ordering = sql_case({id_value: rank for rank, id_value in enumerate(ids)}, value=KbdEntry.id)
    result = await session.execute(select(KbdEntry).where(KbdEntry.id.in_(ids)).order_by(ordering))
    return list(result.scalars().all())


async def _audit_and_serialize(
    session: Any,
    entries: list[KbdEntry],
    scores: dict[int, float],
    *,
    category_id: str,
    query: str,
    top_k: int,
    search_path: str,
    conversation_id: str | None,
    case_id: str | None,
) -> list[dict[str, Any]]:
    """记录候选检索事件并生成响应。"""
    trace_id = get_current_trace_id()
    cases: list[dict[str, Any]] = []
    for rank, entry in enumerate(entries, start=1):
        score = scores[entry.id]
        snapshot = await DynamicResourceLoader(session).get_active("kbd", str(entry.id))
        await DynamicResourceLoader(session).audit_usage(
            snapshot,
            UsageRecord(
                consumer="kb-service.kbd_search",
                status=UsageStatus.RETRIEVED,
                conversation_id=conversation_id,
                case_id=case_id,
                trace_id=trace_id,
                input_payload={"category_id": category_id, "query": query, "top_k": top_k},
                output_payload={"rank": rank, "score": round(score, 6)},
                metadata={
                    "search_path": search_path,
                    "embedding_model": settings.LLM_EMBEDDING_MODEL if search_path == "vector" else None,
                },
            ),
        )
        similarity = score if search_path == "vector" else 0.0
        case = _entry_to_case_dict(entry, similarity)
        case["resource_revision"] = snapshot_revision_metadata(snapshot)
        cases.append(case)
    return cases


async def _vector_candidates(
    session: Any, category_id: str, query_vector: list[float], top_k: int
) -> tuple[list[int], dict[int, float]]:
    """获取通过最低相似度门禁的向量候选。"""
    vector_text = "[" + ",".join(str(value) for value in query_vector) + "]"
    result = await session.execute(
        text(
            f"""
            SELECT id, 1 - (embedding <=> CAST(:query_vector AS vector)) AS score
            FROM kbd_entry
            WHERE category_id = :category_id
              AND status = 'published'
              AND {_USABLE_SIGNALS_SQL}
              AND embedding IS NOT NULL
              AND embedding_model = :embedding_model
              AND 1 - (embedding <=> CAST(:query_vector AS vector)) >= :min_similarity
            ORDER BY embedding <=> CAST(:query_vector AS vector)
            LIMIT :top_k
            """
        ),
        {
            "category_id": category_id,
            "query_vector": vector_text,
            "embedding_model": settings.LLM_EMBEDDING_MODEL,
            "min_similarity": settings.KBD_MIN_SIMILARITY,
            "top_k": top_k,
        },
    )
    rows = result.fetchall()
    ids = [int(row[0]) for row in rows]
    return ids, {int(row[0]): float(row[1]) for row in rows}


async def _fts_candidates(session: Any, category_id: str, query: str, top_k: int) -> tuple[list[int], dict[int, float]]:
    """使用与入库端一致的 jieba token 执行 PostgreSQL FTS。"""
    segmented_query = segment(query)
    result = await session.execute(
        text(
            f"""
            SELECT id, ts_rank(tsv, plainto_tsquery('simple', :query)) AS score
            FROM kbd_entry
            WHERE category_id = :category_id
              AND status = 'published'
              AND {_USABLE_SIGNALS_SQL}
              AND tsv @@ plainto_tsquery('simple', :query)
            ORDER BY score DESC, id DESC
            LIMIT :top_k
            """
        ),
        {"category_id": category_id, "query": segmented_query, "top_k": top_k},
    )
    rows = result.fetchall()
    ids = [int(row[0]) for row in rows]
    return ids, {int(row[0]): float(row[1]) for row in rows}


@router.get("/kbd/search")
async def search_kbds(
    request: Request,
    category_id: str = Query(..., min_length=1, max_length=32, description="分类 ID"),
    query: str = Query(..., min_length=1, max_length=500, description="用户问题文本"),
    top_k: int = Query(15, ge=1, le=50, description="最多返回候选数"),
    conversation_id: str | None = Query(None, max_length=64, description="对话 ID，用于审计关联"),
    case_id: str | None = Query(None, max_length=64, description="工单 ID，用于审计关联"),
) -> dict[str, list[dict[str, Any]]]:
    """按向量门禁优先、中文 FTS 降级检索 KBD 候选。"""
    if _db_manager is None or _embedding_service is None:
        raise HTTPException(status_code=503, detail="服务依赖未初始化")

    logger.info(event="kbd_search_start", category_id=category_id, query_len=len(query), top_k=top_k)

    query_vector: list[float] | None = None
    try:
        query_vector = await _embedding_service.embed_for_search(query)
    except Exception as exc:
        logger.warning(event="kbd_search_embedding_failed", error=str(exc), fallback="fts_jieba")

    async with _db_manager.async_session_factory() as session:
        candidate_ids: list[int] = []
        scores: dict[int, float] = {}
        search_path = "vector"
        if query_vector is not None:
            candidate_ids, scores = await _vector_candidates(session, category_id, query_vector, top_k)

        if not candidate_ids:
            search_path = "fts_jieba"
            candidate_ids, scores = await _fts_candidates(session, category_id, query, top_k)

        entries = await _load_entries_in_order(session, candidate_ids)
        cases = await _audit_and_serialize(
            session,
            entries,
            scores,
            category_id=category_id,
            query=query,
            top_k=top_k,
            search_path=search_path,
            conversation_id=conversation_id,
            case_id=case_id,
        )
        await session.commit()

    logger.info(
        event="kbd_search_done",
        category_id=category_id,
        result_count=len(cases),
        search_path=search_path,
        min_similarity=settings.KBD_MIN_SIMILARITY if search_path == "vector" else None,
    )
    return {"cases": cases}
