"""
KB Service — 管理后台路由

提供文档状态机管理接口（审核/发布/下线）和文档列表查询。
仅供管理员使用，需 INTERNAL_API_TOKEN 鉴权。

GET  /api/kb/documents            — 查询文档列表（分页 + 状态过滤）
GET  /api/kb/documents/{id}       — 查询单个文档详情
PATCH /api/kb/documents/{id}      — 更新文档状态（审核通过/发布/归档）
DELETE /api/kb/documents/{id}     — 删除文档（级联删除 chunks）

POST /api/admin/kbd/{id}/approve  — KBD 条目审核通过（生成 embedding + tsv）
POST /api/admin/sop/{id}/approve  — SOP 文档审核通过（遍历 chunks 生成 embedding + tsv）
"""

from __future__ import annotations

import hashlib
import io
import json
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile, status
from pydantic import BaseModel, Field
from shared.observability.logger import get_logger
from shared.observability.otel import get_current_trace_id
from sqlalchemy import select, text

from app.models.document import KBDocument
from app.models.sop_document import SopDocument
from app.services.sop_parser import parse_sop_markdown

if TYPE_CHECKING:
    from shared.database.postgres import DatabaseManager

    from app.services.embedding import EmbeddingService

logger = get_logger("kb-service-admin")
router = APIRouter(prefix="/api/kb", tags=["admin"])

# 新增 KBD 审核路由（独立 prefix）
kbd_router = APIRouter(prefix="/api/admin/kbd", tags=["kbd-admin"])

# 新增 SOP 审核路由（独立 prefix）
sop_router = APIRouter(prefix="/api/admin/sop", tags=["sop-admin"])

_db_manager: DatabaseManager | None = None
_embedding_service: EmbeddingService | None = None


def set_dependencies(db: DatabaseManager, embedding: EmbeddingService | None = None) -> None:
    """注入数据库和 embedding 服务依赖"""
    global _db_manager, _embedding_service
    _db_manager = db
    _embedding_service = embedding


def _check_auth(request: Request) -> None:
    """验证内部服务 Token"""
    from app.config import settings

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="缺少 Bearer Token")
    token = auth_header.split(" ", 1)[1]
    if token != settings.INTERNAL_API_TOKEN:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token 无效")


class DocumentUpdateRequest(BaseModel):
    """文档状态更新请求"""

    status: str | None = None  # draft/under_review/approved/published/rejected/archived
    review_note: str | None = None
    reviewer: str | None = None


@router.get("/documents")
async def list_documents(
    request: Request,
    status_filter: str | None = None,
    category_l1: str | None = None,
    page: int = 1,
    page_size: int = 20,
):
    """查询文档列表（分页 + 状态过滤）"""
    _check_auth(request)

    if _db_manager is None:
        raise HTTPException(status_code=503, detail="数据库未就绪")

    offset = (page - 1) * page_size
    async with _db_manager.async_session_factory() as session:
        query = select(
            KBDocument.id,
            KBDocument.title,
            KBDocument.status,
            KBDocument.source_type,
            KBDocument.category_l1,
            KBDocument.category_l2,
            KBDocument.difficulty,
            KBDocument.created_at,
        )
        if status_filter:
            query = query.where(KBDocument.status == status_filter)
        if category_l1:
            query = query.where(KBDocument.category_l1 == category_l1)
        query = query.order_by(KBDocument.created_at.desc()).offset(offset).limit(page_size)

        result = await session.execute(query)
        rows = result.fetchall()

    return {
        "page": page,
        "page_size": page_size,
        "documents": [
            {
                "id": r.id,
                "title": r.title,
                "status": r.status,
                "source_type": r.source_type,
                "category_l1": r.category_l1,
                "category_l2": r.category_l2,
                "difficulty": r.difficulty,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ],
    }


@router.get("/documents/{doc_id}")
async def get_document(request: Request, doc_id: int):
    """查询单个文档详情"""
    _check_auth(request)

    if _db_manager is None:
        raise HTTPException(status_code=503, detail="数据库未就绪")

    async with _db_manager.async_session_factory() as session:
        result = await session.execute(select(KBDocument).where(KBDocument.id == doc_id))
        doc = result.scalar_one_or_none()

    if not doc:
        raise HTTPException(status_code=404, detail=f"文档 {doc_id} 不存在")

    return {
        "id": doc.id,
        "title": doc.title,
        "status": doc.status,
        "source_id": doc.source_id,
        "source_type": doc.source_type,
        "category_l1": doc.category_l1,
        "category_l2": doc.category_l2,
        "tags": doc.tags,
        "summary": doc.summary,
        "judgment_logic": doc.judgment_logic,
        "difficulty": doc.difficulty,
        "review_note": doc.review_note,
        "reviewer": doc.reviewer,
        "reviewed_at": doc.reviewed_at.isoformat() if doc.reviewed_at else None,
        "created_at": doc.created_at.isoformat(),
    }


@router.patch("/documents/{doc_id}")
async def update_document(request: Request, doc_id: int, body: DocumentUpdateRequest):
    """更新文档状态（审核/发布/归档）"""
    _check_auth(request)

    if _db_manager is None:
        raise HTTPException(status_code=503, detail="数据库未就绪")

    from datetime import UTC, datetime

    # 验证状态合法性
    if body.status and body.status not in KBDocument.VALID_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"非法状态: {body.status}，合法值: {KBDocument.VALID_STATUSES}",
        )

    async with _db_manager.async_session_factory() as session:
        result = await session.execute(select(KBDocument).where(KBDocument.id == doc_id))
        doc = result.scalar_one_or_none()
        if not doc:
            raise HTTPException(status_code=404, detail=f"文档 {doc_id} 不存在")

        if body.status:
            doc.status = body.status
            # 记录审核信息
            if body.status in {"approved", "published"}:
                doc.reviewed_at = datetime.now(UTC)
        if body.review_note is not None:
            doc.review_note = body.review_note
        if body.reviewer is not None:
            doc.reviewer = body.reviewer

        await session.commit()

    logger.info(event="document_updated", doc_id=doc_id, new_status=body.status)
    return {"id": doc_id, "status": body.status or doc.status, "updated": True}


@router.delete("/documents/{doc_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(request: Request, doc_id: int):
    """删除文档（级联删除关联 chunks）"""
    _check_auth(request)

    if _db_manager is None:
        raise HTTPException(status_code=503, detail="数据库未就绪")

    async with _db_manager.async_session_factory() as session:
        result = await session.execute(select(KBDocument).where(KBDocument.id == doc_id))
        doc = result.scalar_one_or_none()
        if not doc:
            raise HTTPException(status_code=404, detail=f"文档 {doc_id} 不存在")
        await session.delete(doc)
        await session.commit()

    logger.info(event="document_deleted", doc_id=doc_id)


# ─────────────────────────────────────────────────────────────────────────────
# KBD 条目列表查询接口（kbd_entry 表）
# ─────────────────────────────────────────────────────────────────────────────


@kbd_router.get("/pending")
async def list_kbd_entries(
    request: Request,
    page: int = 1,
    page_size: int = 20,
    status: str = "draft",
    category_id: str | None = None,
    support_id: str | None = None,
    title_keyword: str | None = None,
):
    """查询 KBD 条目列表（分页 + 状态/分类/案例ID/标题过滤）

    Args:
        page: 页码（从 1 开始）
        page_size: 每页条数（最大 100）
        status: 状态过滤（draft/published/rejected/archived）
        category_id: 按 AI 分类 ID 过滤（可选）
        support_id: 按案例 ID 精准匹配（可选）
        title_keyword: 按标题关键字模糊搜索（可选）

    Returns:
        { entries: [...], total, page, page_size }
    """
    _check_auth(request)

    if _db_manager is None:
        raise HTTPException(status_code=503, detail="数据库未就绪")

    # 参数校验
    page_size = min(max(page_size, 1), 100)
    page = max(page, 1)
    offset = (page - 1) * page_size

    logger.info(
        event="kbd_list_request",
        page=page,
        page_size=page_size,
        status=status,
        category_id=category_id,
        support_id=support_id,
        title_keyword=title_keyword,
    )

    async with _db_manager.async_session_factory() as session:
        # 构建 WHERE 条件
        where_clauses = ["status = :status"]
        params: dict = {"status": status, "limit": page_size, "offset": offset}

        if category_id:
            where_clauses.append("(ai_category_id = :category_id OR category_id = :category_id)")
            params["category_id"] = category_id

        # 按案例 ID 精准匹配
        if support_id:
            where_clauses.append("support_id = :support_id")
            params["support_id"] = support_id

        # 按标题关键字模糊搜索
        if title_keyword:
            where_clauses.append("title ILIKE :title_keyword")
            params["title_keyword"] = f"%{title_keyword}%"

        where_sql = " AND ".join(where_clauses)

        # 查询总数
        count_sql = text(f"SELECT COUNT(*) FROM kbd_entry WHERE {where_sql}")  # noqa: S608
        count_result = await session.execute(count_sql, params)
        total = count_result.scalar() or 0

        # 查询分页数据
        data_sql = text(  # noqa: S608
            f"""
            SELECT id, support_id, title,
                   problem_description, alert_info, steps_text, root_cause,
                   solution, operational_impact, is_temporary, recommendations,
                   steps_json, content_md,
                   metadata, category_id, ai_category_id,
                   ai_category_conf, ai_category_reason,
                   status, reviewer_id, review_note,
                   hit_count, created_at, updated_at
            FROM kbd_entry
            WHERE {where_sql}
            ORDER BY updated_at DESC, id DESC
            LIMIT :limit OFFSET :offset
            """
        )
        result = await session.execute(data_sql, params)
        rows = result.mappings().all()

    entries = [
        {
            "id": row["id"],
            "support_id": row["support_id"],
            "title": row["title"],
            "problem_description": row["problem_description"] or "",
            "alert_info": row["alert_info"] or "",
            "steps_text": row["steps_text"] or "",
            "root_cause": row["root_cause"] or "",
            "solution": row["solution"] or "",
            "operational_impact": row["operational_impact"] or "",
            "is_temporary": row["is_temporary"] or "",
            "recommendations": row["recommendations"] or "",
            "steps_json": row["steps_json"] or [],
            "content_md": row["content_md"] or "",
            "metadata": row["metadata"] or {},
            "category_id": row["category_id"],
            "ai_category_id": row["ai_category_id"],
            "ai_category_conf": float(row["ai_category_conf"]) if row["ai_category_conf"] is not None else None,
            "ai_category_reason": row["ai_category_reason"],
            "status": row["status"],
            "reviewer_id": row["reviewer_id"],
            "review_note": row["review_note"],
            "hit_count": row.get("hit_count", 0),
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
            "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
        }
        for row in rows
    ]

    logger.info(event="kbd_list_response", total=total, returned=len(entries))

    return {
        "entries": entries,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# ─────────────────────────────────────────────────────────────────────────────
# KBD 条目单条详情接口
# ─────────────────────────────────────────────────────────────────────────────


@kbd_router.get("/{kbd_id}")
async def get_kbd_entry_detail(request: Request, kbd_id: int):
    """获取单个 KBD 条目详情（含完整 content_md）

    Args:
        kbd_id: KBD 条目 ID

    Returns:
        KBD 条目完整详情（含 content_md、metadata 等）
    """
    _check_auth(request)

    if _db_manager is None:
        raise HTTPException(status_code=503, detail="数据库未就绪")

    logger.info(event="kbd_detail_request", kbd_id=kbd_id, trace_id=get_current_trace_id())

    async with _db_manager.async_session_factory() as session:
        result = await session.execute(
            text("""
                SELECT id, support_id, title,
                       problem_description, alert_info, steps_text, root_cause,
                       solution, operational_impact, is_temporary, recommendations,
                       steps_json, content_md,
                       metadata, category_id, ai_category_id,
                       ai_category_conf, ai_category_reason,
                       status, reviewer_id, review_note,
                       created_at, updated_at, published_at
                FROM kbd_entry
                WHERE id = :id
            """),
            {"id": kbd_id},
        )
        row = result.mappings().first()

        if not row:
            raise HTTPException(status_code=404, detail=f"KBD 条目 {kbd_id} 不存在")

    return {
        "id": row["id"],
        "support_id": row["support_id"],
        "title": row["title"],
        # 8 大章节字段（完整内容）
        "problem_description": row["problem_description"] or "",
        "alert_info": row["alert_info"] or "",
        "steps_text": row["steps_text"] or "",
        "root_cause": row["root_cause"] or "",
        "solution": row["solution"] or "",
        "operational_impact": row["operational_impact"] or "",
        "is_temporary": row["is_temporary"] or "",
        "recommendations": row["recommendations"] or "",
        "steps_json": row["steps_json"] or [],
        "content_md": row["content_md"] or "",
        "metadata": row["metadata"] or {},
        "category_id": row["category_id"],
        "ai_category_id": row["ai_category_id"],
        "ai_category_conf": float(row["ai_category_conf"]) if row["ai_category_conf"] is not None else None,
        "ai_category_reason": row["ai_category_reason"],
        "status": row["status"],
        "reviewer_id": row["reviewer_id"],
        "review_note": row["review_note"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
        "published_at": row["published_at"].isoformat() if row["published_at"] else None,
    }


# ─────────────────────────────────────────────────────────────────────────────
# KBD 条目拒绝接口
# ─────────────────────────────────────────────────────────────────────────────


class KbdRejectRequest(BaseModel):
    """KBD 条目拒绝请求"""

    reviewer_id: int = Field(..., description="审核人 ID")
    review_note: str = Field(..., min_length=1, max_length=500, description="拒绝原因（必填）")


@kbd_router.patch("/{kbd_id}/reject")
async def reject_kbd_entry(request: Request, kbd_id: int, body: KbdRejectRequest):
    """拒绝 KBD 条目，更新状态为 rejected"""
    _check_auth(request)

    if _db_manager is None:
        raise HTTPException(status_code=503, detail="数据库未就绪")

    logger.info(event="kbd_reject_request", kbd_id=kbd_id, reviewer_id=body.reviewer_id)

    now = datetime.now(UTC)
    async with _db_manager.async_session_factory() as session:
        result = await session.execute(
            text(
                """
                UPDATE kbd_entry
                SET status = 'rejected',
                    reviewer_id = :reviewer_id,
                    reviewed_at = :reviewed_at,
                    review_note = :review_note
                WHERE id = :id AND status = 'draft'
                RETURNING id, status
                """
            ),
            {
                "id": kbd_id,
                "reviewer_id": body.reviewer_id,
                "reviewed_at": now,
                "review_note": body.review_note,
            },
        )
        updated = result.mappings().first()
        if not updated:
            raise HTTPException(status_code=404, detail=f"KBD 条目 {kbd_id} 不存在或状态非 draft")
        await session.commit()

    logger.info(event="kbd_rejected", kbd_id=kbd_id)
    return {"success": True, "kbd_id": kbd_id, "status": "rejected"}


class KbdApproveRequest(BaseModel):
    """KBD 条目审核通过请求"""

    reviewer_id: int = Field(..., ge=1, description="审核人 ID")
    review_note: str | None = Field(None, max_length=500, description="审核备注（可选）")


class KbdApproveResponse(BaseModel):
    """KBD 条目审核通过响应"""

    success: bool = Field(..., description="操作是否成功")
    kbd_id: int = Field(..., description="KBD 条目 ID")
    status: str = Field(..., description="当前状态")
    embedding_generated: bool = Field(..., description="是否成功生成 embedding")
    published_at: str | None = Field(None, description="发布时间")


@kbd_router.post("/{kbd_id}/approve", response_model=KbdApproveResponse)
async def approve_kbd_entry(request: Request, kbd_id: int, body: KbdApproveRequest):
    """审核通过 KBD 条目

    功能清单：
    1. 更新 kbd_entry.status → published
    2. 触发 embedding 生成（调用 embedding API 对 content_md 生成向量）
    3. 生成 tsv tsvector（BM25 索引，使用 to_tsvector('simple', content_md)）
    4. 设置 published_at = NOW()
    5. 记录 reviewer_id

    响应体示例：
    ```json
    {
      "success": true,
      "kbd_id": 123,
      "status": "published",
      "embedding_generated": true,
      "published_at": "2026-04-02T10:30:00Z"
    }
    ```
    """
    _check_auth(request)

    if _db_manager is None:
        raise HTTPException(status_code=503, detail="数据库未就绪")

    logger.info(
        event="kbd_approve_request",
        kbd_id=kbd_id,
        reviewer_id=body.reviewer_id,
    )

    # 1. 查询 kbd_entry（短事务，快速释放连接）
    async with _db_manager.async_session_factory() as session:
        result = await session.execute(
            text("SELECT id, title, content_md, problem_description, alert_info, root_cause, status, published_at, embedding FROM kbd_entry WHERE id = :id"),
            {"id": kbd_id},
        )
        row = result.mappings().first()

        if not row:
            raise HTTPException(status_code=404, detail=f"KBD 条目 {kbd_id} 不存在")

        current_status = row["status"]
        if current_status == "published":
            # 已发布，无需重复处理
            return KbdApproveResponse(
                success=True,
                kbd_id=kbd_id,
                status="published",
                embedding_generated=row["embedding"] is not None,
                published_at=row["published_at"].isoformat() if row["published_at"] else None,
            )

        content_md = row["content_md"]
        if not content_md:
            raise HTTPException(
                status_code=400,
                detail=f"KBD 条目 {kbd_id} 缺少 content_md，无法生成 embedding",
            )
        # 构建 embedding 输入（问题侧字段，避免答案侧污染向量空间）
        embedding_text = "\n\n".join(filter(None, [
            row["title"],
            row["problem_description"],
            row["alert_info"],
            row["root_cause"],
        ]))
        if not embedding_text.strip():
            embedding_text = content_md  # 降级：章节字段均空时用 content_md

    # 2. 生成 embedding（事务外调用，避免长时间占用连接）
    embedding_generated = False
    embedding_vector: list[float] | None = None
    if _embedding_service:
        try:
            embedding_vector = await _embedding_service.embed_single(embedding_text)
            embedding_generated = True

            # 检查向量维度是否与数据库一致
            expected_dim = 1536
            actual_dim = len(embedding_vector)
            if actual_dim != expected_dim:
                logger.warning(
                    event="kbd_embedding_dim_mismatch",
                    kbd_id=kbd_id,
                    expected_dim=expected_dim,
                    actual_dim=actual_dim,
                    message=f"向量维度不匹配（期望 {expected_dim}，实际 {actual_dim}）",
                )

            logger.info(
                event="kbd_embedding_generated",
                kbd_id=kbd_id,
                vector_dim=actual_dim,
            )
        except Exception as exc:
            logger.warning(
                event="kbd_embedding_failed",
                kbd_id=kbd_id,
                error=str(exc),
                message="embedding 生成失败，将继续更新状态，后续可手动重试",
            )

    # 3. 更新 kbd_entry 状态（短事务）
    now = datetime.now(UTC)
    async with _db_manager.async_session_factory() as session:
        # 构建 UPDATE SQL（embedding 使用 pgvector 格式）
        if embedding_vector:
            # 将向量列表转换为 PostgreSQL vector 格式字符串
            vector_str = "[" + ",".join(str(v) for v in embedding_vector) + "]"
            update_sql = text(
                """
                UPDATE kbd_entry
                SET status = 'published',
                    published_at = :published_at,
                    reviewer_id = :reviewer_id,
                    reviewed_at = :reviewed_at,
                    review_note = COALESCE(:review_note, review_note),
                    embedding = CAST(:embedding AS vector),
                    tsv = to_tsvector('simple', COALESCE(title, '') || ' ' || COALESCE(content_md, ''))
                WHERE id = :id
                RETURNING id, status, embedding, published_at
                """
            )
            params = {
                "id": kbd_id,
                "published_at": now,
                "reviewer_id": body.reviewer_id,
                "reviewed_at": now,
                "review_note": body.review_note,
                "embedding": vector_str,
            }
        else:
            # 无 embedding，仅更新状态和 tsv
            update_sql = text(
                """
                UPDATE kbd_entry
                SET status = 'published',
                    published_at = :published_at,
                    reviewer_id = :reviewer_id,
                    reviewed_at = :reviewed_at,
                    review_note = COALESCE(:review_note, review_note),
                    tsv = to_tsvector('simple', COALESCE(title, '') || ' ' || COALESCE(content_md, ''))
                WHERE id = :id
                RETURNING id, status, embedding, published_at
                """
            )
            params = {
                "id": kbd_id,
                "published_at": now,
                "reviewer_id": body.reviewer_id,
                "reviewed_at": now,
                "review_note": body.review_note,
            }

        result = await session.execute(update_sql, params)
        updated = result.mappings().first()

        if not updated:
            raise HTTPException(status_code=500, detail=f"KBD 条目 {kbd_id} 更新失败")

        await session.commit()

    logger.info(
        event="kbd_approved",
        kbd_id=kbd_id,
        reviewer_id=body.reviewer_id,
        embedding_generated=embedding_generated,
    )

    return KbdApproveResponse(
        success=True,
        kbd_id=kbd_id,
        status=updated["status"],
        embedding_generated=updated["embedding"] is not None,
        published_at=updated["published_at"].isoformat() if updated["published_at"] else None,
    )


# ─────────────────────────────────────────────────────────────────────────────
# SOP 文档审核接口（sop_document 单表，tree_json 合并存储）
# ─────────────────────────────────────────────────────────────────────────────


class SopApproveRequest(BaseModel):
    """SOP 文档审核通过请求"""

    reviewer_id: int = Field(..., ge=1, description="审核人 ID")
    review_note: str | None = Field(None, max_length=500, description="审核备注（可选）")


class SopApproveResponse(BaseModel):
    """SOP 文档审核通过响应"""

    success: bool = Field(..., description="操作是否成功")
    document_id: int = Field(..., description="SOP 文档 ID")
    status: str = Field(..., description="当前状态")
    chunks_embedded: int = Field(0, description="已废弃字段（sop_chunk 已删除），始终为 0")
    tree_generated: bool = Field(..., description="是否成功生成 SOP 决策树")
    tree_leaf_count: int | None = Field(None, description="决策树叶节点数量")
    tree_validation_status: str | None = Field(None, description="决策树校验状态（valid/warnings）")
    published_at: str | None = Field(None, description="发布时间")


@sop_router.post("/{document_id}/approve", response_model=SopApproveResponse)
async def approve_sop_document(request: Request, document_id: int, body: SopApproveRequest):
    """审核通过 SOP 文档

    功能清单：
    1. 更新 sop_document.status → published
    2. 解析 content_md 生成 SOP 决策树（SOPNode JSON），写入 sop_document.tree_json
    3. 设置 published_at = NOW()
    4. 记录 reviewer_id

    两段式事务设计（解析树不持有 DB 连接）：
      - 短事务1：查询 document（验证存在）
      - 无事务：解析 SOP Markdown 生成决策树（无 IO 操作，但解析可能耗时）
      - 短事务2：UPDATE sop_document（状态 + tree_json）

    注意：sop_chunk 表已废弃，chunks_embedded 字段始终为 0（向后兼容保留）。

    响应体示例：
    ```json
    {
      "success": true,
      "document_id": 1,
      "status": "published",
      "chunks_embedded": 5,
      "published_at": "2026-04-02T10:30:00Z"
    }
    ```
    """
    _check_auth(request)

    if _db_manager is None:
        raise HTTPException(status_code=503, detail="数据库未就绪")

    logger.info(
        event="sop_approve_request",
        document_id=document_id,
        reviewer_id=body.reviewer_id,
    )

    try:
        # ── 短事务1：查询验证（快速释放连接）────────────────────────────────────
        async with _db_manager.async_session_factory() as session:
            result = await session.execute(select(SopDocument).where(SopDocument.id == document_id))
            sop_doc = result.scalar_one_or_none()

            if not sop_doc:
                raise HTTPException(status_code=404, detail=f"SOP 文档 {document_id} 不存在")

            if sop_doc.status == "published":
                # 已发布，直接返回当前 tree 信息
                return SopApproveResponse(
                    success=True,
                    document_id=document_id,
                    status="published",
                    chunks_embedded=0,
                    tree_generated=sop_doc.tree_json is not None,
                    tree_leaf_count=sop_doc.tree_leaf_count if sop_doc.tree_json is not None else None,
                    tree_validation_status=sop_doc.tree_validation_status if sop_doc.tree_json is not None else None,
                    published_at=sop_doc.published_at.isoformat() if sop_doc.published_at else None,
                )

            content_md = sop_doc.content_md

        # ── 无事务：解析 SOP 决策树（不持有 DB 连接）──────────────────────────
        now = datetime.now(UTC)
        tree_generated = False
        tree_leaf_count = 0
        tree_validation_status: str | None = None

        if content_md:
            parse_result = parse_sop_markdown(content_md)
            if parse_result.is_valid and parse_result.tree:
                tree_generated = True
                tree_leaf_count = len(parse_result.tree.collect_leaves())
                tree_validation_status = "warnings" if parse_result.warnings else "valid"
                logger.info(
                    event="sop_tree_parsed",
                    document_id=document_id,
                    leaf_count=tree_leaf_count,
                    warning_count=len(parse_result.warnings),
                )
            else:
                tree_validation_status = "error"
                logger.warning(
                    event="sop_tree_parse_failed",
                    document_id=document_id,
                    error_count=len(parse_result.errors),
                    errors=[e.message for e in parse_result.errors[:3]],
                )
        else:
            parse_result = None
            logger.warning(
                event="sop_tree_no_content",
                document_id=document_id,
                message="SOP 文档没有 content_md，无法生成决策树",
            )

        # ── 短事务2：UPDATE sop_document（状态 + tree_json）────────────────────
        async with _db_manager.async_session_factory() as session:
            # 更新状态字段
            await session.execute(
                text(
                    """
                    UPDATE sop_document
                    SET status = 'published',
                        published_at = :published_at,
                        reviewer_id = :reviewer_id,
                        reviewed_at = :reviewed_at,
                        review_note = COALESCE(:review_note, review_note)
                    WHERE id = :id
                    """
                ),
                {
                    "id": document_id,
                    "published_at": now,
                    "reviewer_id": body.reviewer_id,
                    "reviewed_at": now,
                    "review_note": body.review_note,
                },
            )

            # 写入决策树（若解析成功）
            if parse_result and parse_result.is_valid and parse_result.tree:
                await session.execute(
                    text(
                        """
                        UPDATE sop_document
                        SET tree_json              = CAST(:tree_json AS jsonb),
                            tree_schema_version    = :schema_version,
                            tree_leaf_count        = :leaf_count,
                            tree_validation_status = :validation_status,
                            tree_validation_issues = CAST(:validation_issues AS jsonb),
                            tree_generator_version = :generator_version,
                            updated_at             = :updated_at
                        WHERE id = :document_id
                        """
                    ),
                    {
                        "document_id": document_id,
                        "tree_json": json.dumps(parse_result.tree.model_dump(), ensure_ascii=False),
                        "schema_version": "sop-tree-v1",
                        "leaf_count": tree_leaf_count,
                        "validation_status": tree_validation_status,
                        "validation_issues": json.dumps([w.model_dump() for w in parse_result.warnings], ensure_ascii=False)
                        if parse_result.warnings
                        else None,
                        "generator_version": "sop-parser-v1",
                        "updated_at": now,
                    },
                )
                logger.info(
                    event="sop_tree_written",
                    document_id=document_id,
                    leaf_count=tree_leaf_count,
                )

            await session.commit()

    except HTTPException:
        raise
    except Exception as exc:
        logger.exception(
            "发布 SOP 文档时发生未预期异常",
            event="sop_approve_unexpected_error",
            document_id=document_id,
            error_type=type(exc).__name__,
        )
        raise HTTPException(
            status_code=500,
            detail="发布 SOP 文档失败，请联系管理员或查看服务日志",
        ) from exc

    logger.info(
        event="sop_approved",
        document_id=document_id,
        reviewer_id=body.reviewer_id,
        tree_generated=tree_generated,
    )

    return SopApproveResponse(
        success=True,
        document_id=document_id,
        status="published",
        chunks_embedded=0,
        tree_generated=tree_generated,
        tree_leaf_count=tree_leaf_count if tree_generated else None,
        tree_validation_status=tree_validation_status if tree_generated else None,
        published_at=now.isoformat(),
    )


# ─────────────────────────────────────────────────────────────────────────────
# SOP 文档单条详情查询（含 content_md）
# ─────────────────────────────────────────────────────────────────────────────


@sop_router.get("/{document_id}")
async def get_sop_document(request: Request, document_id: int):
    """获取单个 SOP 文档详情（含 content_md 正文）"""
    _check_auth(request)

    if _db_manager is None:
        raise HTTPException(status_code=503, detail="数据库未就绪")

    async with _db_manager.async_session_factory() as session:
        row = (
            (
                await session.execute(
                    text(
                        """
                SELECT id, source_id, category_id, title, content_md, status,
                       reviewer_id, reviewed_at, published_at, created_at, updated_at,
                       tree_leaf_count, (tree_json IS NOT NULL) AS has_tree
                FROM sop_document WHERE id = :id
                """
                    ),
                    {"id": document_id},
                )
            )
            .mappings()
            .first()
        )

    if not row:
        raise HTTPException(status_code=404, detail=f"SOP 文档 {document_id} 不存在")

    return {
        "id": row["id"],
        "source_id": row["source_id"],
        "category_id": row["category_id"],
        "title": row["title"],
        "content_md": row["content_md"],
        "status": row["status"],
        "tree_leaf_count": row["tree_leaf_count"],
        "has_tree": row["has_tree"],
        "reviewer_id": row["reviewer_id"],
        "reviewed_at": row["reviewed_at"].isoformat() if row["reviewed_at"] else None,
        "published_at": row["published_at"].isoformat() if row["published_at"] else None,
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
    }


# ─────────────────────────────────────────────────────────────────────────────
# SOP 文档列表查询接口
# ─────────────────────────────────────────────────────────────────────────────


@sop_router.get("")
async def list_sop_documents(
    request: Request,
    page: int = 1,
    page_size: int = 20,
    status: str | None = None,
    category_id: str | None = None,
):
    """查询 SOP 文档列表（分页 + 状态/分类过滤）

    Returns:
        { documents: [...], total, page, page_size }
    """
    _check_auth(request)

    if _db_manager is None:
        raise HTTPException(status_code=503, detail="数据库未就绪")

    page_size = min(max(page_size, 1), 100)
    page = max(page, 1)
    offset = (page - 1) * page_size

    async with _db_manager.async_session_factory() as session:
        where_clauses = []
        params: dict = {"limit": page_size, "offset": offset}

        if status:
            where_clauses.append("status = :status")
            params["status"] = status
        if category_id:
            where_clauses.append("category_id = :category_id")
            params["category_id"] = category_id

        where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

        count_sql = text(f"SELECT COUNT(*) FROM sop_document {where_sql}")  # noqa: S608
        count_result = await session.execute(count_sql, params)
        total = count_result.scalar() or 0

        data_sql = text(  # noqa: S608
            f"""
            SELECT id, source_id, category_id, title, status,
                   reviewer_id, reviewed_at, published_at, created_at, updated_at, hit_count,
                   tree_leaf_count, (tree_json IS NOT NULL) AS has_tree
            FROM sop_document
            {where_sql}
            ORDER BY created_at DESC, id DESC
            LIMIT :limit OFFSET :offset
            """
        )
        result = await session.execute(data_sql, params)
        rows = result.mappings().all()

    documents = [
        {
            "id": row["id"],
            "source_id": row["source_id"],
            "category_id": row["category_id"],
            "title": row["title"],
            "status": row["status"],
            "tree_leaf_count": row["tree_leaf_count"],
            "has_tree": row["has_tree"],
            "reviewer_id": row["reviewer_id"],
            "reviewed_at": row["reviewed_at"].isoformat() if row["reviewed_at"] else None,
            "published_at": row["published_at"].isoformat() if row["published_at"] else None,
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
            "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
            "hit_count": row.get("hit_count", 0),
        }
        for row in rows
    ]

    return {
        "documents": documents,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# ─────────────────────────────────────────────────────────────────────────────
# SOP 文档状态更新接口（下线/归档）
# ─────────────────────────────────────────────────────────────────────────────


class SopStatusUpdateRequest(BaseModel):
    """SOP 文档状态/信息更新请求"""

    status: str | None = Field(None, description="目标状态：archived 等")
    title: str | None = Field(None, max_length=500, description="新标题（可选）")
    category_id: str | None = Field(None, max_length=32, description="新分类 ID（可选，传空字符串清除）")
    content_md: str | None = Field(None, description="更新后的 Markdown 正文（可选，修改后将重新分块）")


@sop_router.patch("/{document_id}")
async def update_sop_status(request: Request, document_id: int, body: SopStatusUpdateRequest):
    """更新 SOP 文档状态、标题或分类"""
    _check_auth(request)

    if _db_manager is None:
        raise HTTPException(status_code=503, detail="数据库未就绪")

    if body.status is not None and body.status not in SopDocument.VALID_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"非法状态: {body.status}，合法值: {list(SopDocument.VALID_STATUSES)}",
        )

    if body.status is None and body.title is None and body.category_id is None and body.content_md is None:
        raise HTTPException(status_code=400, detail="至少需要提供一个更新字段")

    rechunked = False
    downgraded_to_draft = False

    async with _db_manager.async_session_factory() as session:
        result = await session.execute(select(SopDocument).where(SopDocument.id == document_id))
        sop_doc = result.scalar_one_or_none()
        if not sop_doc:
            raise HTTPException(status_code=404, detail=f"SOP 文档 {document_id} 不存在")

        if body.status is not None:
            sop_doc.status = body.status
        if body.title is not None:
            sop_doc.title = body.title
        if body.category_id is not None:
            # 传空字符串表示清除分类
            sop_doc.category_id = body.category_id or None

        if body.content_md is not None:
            sop_doc.content_md = body.content_md
            # 内容变更后清空决策树（需重新发布生成）
            sop_doc.tree_json = None
            sop_doc.tree_validation_status = None
            rechunked = True
            # 内容变更后若已发布则降级为草稿
            if sop_doc.status == "published" and body.status is None:
                sop_doc.status = "draft"
                downgraded_to_draft = True

        await session.commit()

    logger.info(
        event="sop_updated",
        document_id=document_id,
        new_status=sop_doc.status,
        new_title=body.title,
        content_updated=rechunked,
        downgraded=downgraded_to_draft,
    )
    resp = {"success": True, "document_id": document_id, "status": sop_doc.status}
    if downgraded_to_draft:
        resp["message"] = "内容已更新，决策树已清空，文档已降级为草稿，请重新发布"
    return resp


# ─────────────────────────────────────────────────────────────────────────────
# KBD 条目内容编辑接口
# ─────────────────────────────────────────────────────────────────────────────


class KbdUpdateRequest(BaseModel):
    """KBD 条目内容编辑请求

    支持编辑标题、8 大章节字段、steps_json 和分类。
    编辑章节字段后，content_md 自动由章节重建（不含视觉描述）。
    若明确传入 content_md，则优先使用传入的值。
    """

    title: str | None = Field(None, max_length=500, description="新标题（可选）")
    # 8 大章节字段
    problem_description: str | None = Field(None, description="问题描述章节")
    alert_info: str | None = Field(None, description="告警信息章节")
    steps_text: str | None = Field(None, description="有效排查步骤（自然语言 Markdown）")
    root_cause: str | None = Field(None, description="根因章节")
    solution: str | None = Field(None, description="解决方案章节")
    operational_impact: str | None = Field(None, description="操作影响范围章节")
    is_temporary: str | None = Field(None, description="是否是临时解决方案章节")
    recommendations: str | None = Field(None, description="建议与总结章节")
    # 结构化工具步骤（agent 可执行）
    steps_json: list[dict] | None = Field(
        None,
        description="结构化工具步骤（[{tool_name, tool_args_template, expected_pattern}]）",
    )
    # 聚合渲染（可选，不传则自动由章节重建）
    content_md: str | None = Field(None, description="聚合 Markdown（优先用传入的值；不传则自动由章节重建）")
    category_id: str | None = Field(None, description="新分类 ID（可选）")


@kbd_router.patch("/{kbd_id}")
async def update_kbd_entry(request: Request, kbd_id: int, body: KbdUpdateRequest):
    """编辑 KBD 条目的标题、章节字段、steps_json 或分类。

    处理逻辑：
    1. 章节字段任意一个被修改时，如果没有明确提供 content_md，则自动先从数据库读取当前章节状态并重建 content_md
    2. 如果明确提供了 content_md，则用传入的值（优先）
    """
    _check_auth(request)

    if _db_manager is None:
        raise HTTPException(status_code=503, detail="数据库未就绪")

    # 所有可更新字段
    section_fields = (
        "problem_description", "alert_info", "steps_text", "root_cause",
        "solution", "operational_impact", "is_temporary", "recommendations",
    )
    any_section_changed = any(getattr(body, f) is not None for f in section_fields)
    has_any_field = (
        body.title is not None
        or any_section_changed
        or body.steps_json is not None
        or body.content_md is not None
        or body.category_id is not None
    )
    if not has_any_field:
        raise HTTPException(status_code=400, detail="至少需要提供一个可更新字段")

    set_clauses = []
    params: dict = {"id": kbd_id}

    if body.title is not None:
        set_clauses.append("title = :title")
        params["title"] = body.title

    for field in section_fields:
        val = getattr(body, field)
        if val is not None:
            set_clauses.append(f"{field} = :{field}")
            params[field] = val

    if body.steps_json is not None:
        set_clauses.append("steps_json = :steps_json::jsonb")
        params["steps_json"] = json.dumps(body.steps_json, ensure_ascii=False)

    # content_md 处理：明确传入则用传入的值；有章节更改则需先读库并重建
    if body.content_md is not None:
        # 明确传入了 content_md，优先使用
        set_clauses.append("content_md = :content_md")
        params["content_md"] = body.content_md
    elif any_section_changed:
        # 章节有变更且未传入 content_md：读库 + 应用 patch + 重建
        async with _db_manager.async_session_factory() as session:
            cur_result = await session.execute(
                text(
                    "SELECT problem_description, alert_info, steps_text, root_cause, "
                    "solution, operational_impact, is_temporary, recommendations "
                    "FROM kbd_entry WHERE id = :id"
                ),
                {"id": kbd_id},
            )
            cur_row = cur_result.mappings().first()
            if not cur_row:
                raise HTTPException(status_code=404, detail=f"KBD 条目 {kbd_id} 不存在")

        # 合并：优先用 body 中的新值，其次用库中现有值
        section_map = {
            "problem_description": "问题描述",
            "alert_info": "告警信息",
            "steps_text": "有效排查步骤",
            "root_cause": "根因",
            "solution": "解决方案",
            "operational_impact": "操作影响范围",
            "is_temporary": "是否是临时解决方案",
            "recommendations": "建议与总结",
        }
        merged_parts = []
        for field, heading in section_map.items():
            # body 中有新值用新值，否则用库中现有值
            text_val = (getattr(body, field) if getattr(body, field) is not None else cur_row[field] or "").strip()
            if text_val:
                merged_parts.append(f"## {heading}\n\n{text_val}")
        rebuilt_content_md = "\n\n".join(merged_parts)
        set_clauses.append("content_md = :content_md")
        params["content_md"] = rebuilt_content_md

    if body.category_id is not None:
        set_clauses.append("category_id = :category_id")
        params["category_id"] = body.category_id

    set_sql = ", ".join(set_clauses)

    async with _db_manager.async_session_factory() as session:
        result = await session.execute(
            text(f"UPDATE kbd_entry SET {set_sql} WHERE id = :id RETURNING id, status"),  # noqa: S608
            params,
        )
        updated = result.mappings().first()
        if not updated:
            raise HTTPException(status_code=404, detail=f"KBD 条目 {kbd_id} 不存在")
        await session.commit()

    logger.info(event="kbd_updated", kbd_id=kbd_id, fields=list(params.keys()))
    return {"success": True, "kbd_id": kbd_id}


# ─────────────────────────────────────────────────────────────────────────────
# KBD 条目重新发布接口（rejected → published）
# ─────────────────────────────────────────────────────────────────────────────


@kbd_router.post("/{kbd_id}/republish", response_model=KbdApproveResponse)
async def republish_kbd_entry(request: Request, kbd_id: int, body: KbdApproveRequest):
    """重新发布已拒绝的 KBD 条目（rejected → published），重新生成 embedding"""
    _check_auth(request)

    if _db_manager is None:
        raise HTTPException(status_code=503, detail="数据库未就绪")

    logger.info(event="kbd_republish_request", kbd_id=kbd_id, reviewer_id=body.reviewer_id)

    # 查询条目（允许 rejected 或 draft 状态）
    async with _db_manager.async_session_factory() as session:
        result = await session.execute(
            text("SELECT id, title, content_md, problem_description, alert_info, root_cause, status FROM kbd_entry WHERE id = :id"),
            {"id": kbd_id},
        )
        row = result.mappings().first()
        if not row:
            raise HTTPException(status_code=404, detail=f"KBD 条目 {kbd_id} 不存在")
        if row["status"] not in {"draft", "rejected"}:
            raise HTTPException(
                status_code=400,
                detail=f"KBD 条目当前状态为 {row['status']}，只有 draft/rejected 状态可重新发布",
            )
        content_md = row["content_md"]
        if not content_md:
            raise HTTPException(status_code=400, detail=f"KBD 条目 {kbd_id} 缺少 content_md")
        # 构建 embedding 输入（问题侧字段，避免答案侧污染向量空间）
        embedding_text = "\n\n".join(filter(None, [
            row["title"],
            row["problem_description"],
            row["alert_info"],
            row["root_cause"],
        ]))
        if not embedding_text.strip():
            embedding_text = content_md

    # 生成 embedding（事务外调用）
    embedding_generated = False
    embedding_vector: list[float] | None = None
    if _embedding_service:
        try:
            embedding_vector = await _embedding_service.embed_single(embedding_text)
            embedding_generated = True
            logger.info(event="kbd_republish_embedding_generated", kbd_id=kbd_id, vector_dim=len(embedding_vector))
        except Exception as exc:
            logger.warning(event="kbd_republish_embedding_failed", kbd_id=kbd_id, error=str(exc))

    now = datetime.now(UTC)
    async with _db_manager.async_session_factory() as session:
        if embedding_vector:
            vector_str = "[" + ",".join(str(v) for v in embedding_vector) + "]"
            update_sql = text(
                """
                UPDATE kbd_entry
                SET status = 'published',
                    published_at = :published_at,
                    reviewer_id = :reviewer_id,
                    reviewed_at = :reviewed_at,
                    review_note = COALESCE(:review_note, review_note),
                    embedding = CAST(:embedding AS vector),
                    tsv = to_tsvector('simple', COALESCE(title, '') || ' ' || COALESCE(content_md, ''))
                WHERE id = :id
                RETURNING id, status, embedding, published_at
                """
            )
            params = {
                "id": kbd_id,
                "published_at": now,
                "reviewer_id": body.reviewer_id,
                "reviewed_at": now,
                "review_note": body.review_note,
                "embedding": vector_str,
            }
        else:
            update_sql = text(
                """
                UPDATE kbd_entry
                SET status = 'published',
                    published_at = :published_at,
                    reviewer_id = :reviewer_id,
                    reviewed_at = :reviewed_at,
                    review_note = COALESCE(:review_note, review_note),
                    tsv = to_tsvector('simple', COALESCE(title, '') || ' ' || COALESCE(content_md, ''))
                WHERE id = :id
                RETURNING id, status, embedding, published_at
                """
            )
            params = {
                "id": kbd_id,
                "published_at": now,
                "reviewer_id": body.reviewer_id,
                "reviewed_at": now,
                "review_note": body.review_note,
            }

        result = await session.execute(update_sql, params)
        updated = result.mappings().first()
        if not updated:
            raise HTTPException(status_code=500, detail=f"KBD 条目 {kbd_id} 更新失败")
        await session.commit()

    logger.info(event="kbd_republished", kbd_id=kbd_id, reviewer_id=body.reviewer_id)
    return KbdApproveResponse(
        success=True,
        kbd_id=kbd_id,
        status="published",
        embedding_generated=embedding_generated,
        published_at=updated["published_at"].isoformat() if updated["published_at"] else None,
    )


# ─────────────────────────────────────────────────────────────────────────────
# SOP 文档上传（docx 文件直接导入）
# ─────────────────────────────────────────────────────────────────────────────


def _parse_docx_bytes(content: bytes) -> tuple[str, str, list[tuple[str, str]]]:
    """解析 .docx 二进制内容，返回 (title, full_markdown, chapters)"""
    try:
        from docx import Document  # noqa: PLC0415
    except ImportError as exc:
        raise HTTPException(status_code=503, detail="服务器未安装 python-docx，请联系管理员") from exc

    doc = Document(io.BytesIO(content))

    title = ""
    md_lines: list[str] = []
    chapters: list[tuple[str, str]] = []
    current_chapter_title = "概述"
    current_chapter_lines: list[str] = []

    for para in doc.paragraphs:
        style_name = para.style.name if para.style else ""
        text = para.text.strip()
        if not text:
            continue

        if style_name.startswith("Heading"):
            try:
                level = int(style_name.split()[-1])
            except ValueError:
                level = 1

            if current_chapter_lines:
                chapter_content = "\n".join(current_chapter_lines).strip()
                if chapter_content:
                    chapters.append((current_chapter_title, chapter_content))

            # 为保持与下游 _split_md_chapters 仅按 H1-H3 分章的既有契约一致，
            # 这里将 docx 的深层标题映射为最多三级 Markdown 标题，避免 H4+ 被写入
            # content_md 后无法被后续分块逻辑识别。
            heading_level = min(level, 3)
            heading_prefix = "#" * heading_level
            heading_line = f"{heading_prefix} {text}"
            md_lines.append(heading_line)
            current_chapter_title = text
            current_chapter_lines = [heading_line]

            if level == 1 and not title:
                title = text
        else:
            md_lines.append(text)
            current_chapter_lines.append(text)

    if current_chapter_lines:
        chapter_content = "\n".join(current_chapter_lines).strip()
        if chapter_content:
            chapters.append((current_chapter_title, chapter_content))

    if not title:
        title = "未命名 SOP 文档"

    full_markdown = "\n\n".join(md_lines)
    return title, full_markdown, chapters


def _split_md_chapters(content_md: str) -> list[tuple[str, str]]:
    """按 Markdown 标题分块，并合并无正文的标题章节到后续章节"""
    heading_pattern = re.compile(r"^(#{1,3})\s+(.+)$")
    current_title = "概述"
    current_lines: list[str] = []
    raw_chapters: list[tuple[str, str]] = []

    for line in content_md.split("\n"):
        match = heading_pattern.match(line)
        if match:
            if current_lines:
                content = "\n".join(current_lines).strip()
                if content:
                    raw_chapters.append((current_title, content))
            current_title = match.group(2).strip()
            current_lines = [line]
        else:
            current_lines.append(line)

    if current_lines:
        content = "\n".join(current_lines).strip()
        if content:
            raw_chapters.append((current_title, content))

    # 后处理：将无正文内容（仅含标题行）的章节合并到下一有正文章节
    def _has_body(text: str) -> bool:
        return any(line.strip() and not line.strip().startswith("#") for line in text.split("\n"))

    merged: list[tuple[str, str]] = []
    pending_content = ""
    pending_title = ""

    for title, content in raw_chapters:
        if _has_body(content):
            if pending_content:
                # 将无正文前缀并入当前有正文章节
                merged.append((pending_title, (pending_content + "\n\n" + content).strip()))
                pending_content = ""
                pending_title = ""
            else:
                merged.append((title, content))
        else:
            # 无正文章节，积累为后续章节前缀
            pending_content = (pending_content + "\n\n" + content).strip() if pending_content else content
            pending_title = title

    # 末尾残留的无正文章节（孤立标题）保留
    if pending_content:
        merged.append((pending_title, pending_content))

    return merged if merged else raw_chapters


@sop_router.post("/upload", status_code=status.HTTP_201_CREATED)
async def upload_sop_document(
    request: Request,
    file: UploadFile = File(..., description=".docx 或 .md 文件"),
    category_id: str | None = Form(None, description="分类编码，如 虚拟机-003"),
):
    """直接上传 .docx 或 .md 文件，解析后写入 SOP 草稿

    支持幂等：相同文件内容（SHA256 哈希）不会重复导入。
    上传成功后状态为 draft，需在本页面点击「发布」后方可被 AI 搜索。
    """
    _check_auth(request)

    if _db_manager is None:
        raise HTTPException(status_code=503, detail="数据库未就绪")

    filename = file.filename or ""
    file_ext = filename.lower().split(".")[-1] if "." in filename else ""

    if file_ext not in ("docx", "md"):
        raise HTTPException(status_code=400, detail="仅支持 .docx 或 .md 格式文件")

    content = await file.read()
    if len(content) > 50 * 1024 * 1024:  # 50MB 限制
        raise HTTPException(status_code=400, detail="文件过大，最大支持 50MB")

    file_hash = hashlib.sha256(content).hexdigest()

    # 根据文件类型解析
    try:
        if file_ext == "docx":
            doc_title, content_md, _ = _parse_docx_bytes(content)
        else:  # .md 文件
            content_md = content.decode("utf-8")
            # 从文件名或首行提取标题
            doc_title = filename.rsplit(".", 1)[0] if filename else "未命名 SOP"
            first_line = content_md.split("\n", 1)[0].strip()
            if first_line.startswith("# "):
                doc_title = first_line[2:].strip()
    except Exception as exc:
        logger.error(event="sop_upload_parse_error", filename=filename, error=str(exc))
        raise HTTPException(status_code=400, detail=f"文件解析失败：{exc}") from exc

    docx_hash = file_hash if file_ext == "docx" else None

    async with _db_manager.async_session_factory() as session:
        # 幂等：已存在相同哈希则返回已有文档
        from sqlalchemy import select as sa_select  # noqa: PLC0415

        existing = await session.execute(sa_select(SopDocument).where(SopDocument.docx_hash == docx_hash))
        existing_doc = existing.scalar_one_or_none()
        if existing_doc:
            return {
                "success": True,
                "document_id": existing_doc.id,
                "chunks_created": 0,
                "status": existing_doc.status,
                "duplicate": True,
                "message": f"文件已导入（document_id={existing_doc.id}），跳过重复入库",
            }

        # 新建 sop_document
        sop_doc = SopDocument(
            source_id=f"sop-upload-{file_hash[:12]}",
            title=doc_title,
            content_md=content_md,
            category_id=category_id or None,
            docx_hash=docx_hash,
            status="draft",
        )
        session.add(sop_doc)
        await session.flush()

        await session.commit()

        document_id = sop_doc.id

    logger.info(
        event="sop_upload_completed",
        document_id=document_id,
        title=doc_title[:50],
        filename=filename,
    )
    return {
        "success": True,
        "document_id": document_id,
        "chunks_created": 0,
        "status": "draft",
        "duplicate": False,
        "title": doc_title,
    }
