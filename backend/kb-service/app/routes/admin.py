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

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field
from shared.utils.logger import get_logger
from sqlalchemy import select, text

from app.models.document import KBDocument
from app.models.sop_chunk import SopChunk
from app.models.sop_document import SopDocument

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
):
    """查询 KBD 条目列表（分页 + 状态/分类过滤）

    Args:
        page: 页码（从 1 开始）
        page_size: 每页条数（最大 100）
        status: 状态过滤（draft/published/rejected/archived）
        category_id: 按 AI 分类 ID 过滤（可选）

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
    )

    async with _db_manager.async_session_factory() as session:
        # 构建 WHERE 条件
        where_clauses = ["status = :status"]
        params: dict = {"status": status, "limit": page_size, "offset": offset}

        if category_id:
            where_clauses.append("(ai_category_id = :category_id OR category_id = :category_id)")
            params["category_id"] = category_id

        where_sql = " AND ".join(where_clauses)

        # 查询总数
        count_sql = text(f"SELECT COUNT(*) FROM kbd_entry WHERE {where_sql}")  # noqa: S608
        count_result = await session.execute(count_sql, params)
        total = count_result.scalar() or 0

        # 查询分页数据
        data_sql = text(  # noqa: S608
            f"""
            SELECT id, support_id, support_url, title,
                   LEFT(content_md, 300) AS content_md,
                   metadata, category_id, ai_category_id,
                   ai_category_conf, ai_category_reason,
                   status, reviewer_id, review_note,
                   created_at, updated_at
            FROM kbd_entry
            WHERE {where_sql}
            ORDER BY created_at DESC
            LIMIT :limit OFFSET :offset
            """
        )
        result = await session.execute(data_sql, params)
        rows = result.mappings().all()

    entries = [
        {
            "id": row["id"],
            "support_id": row["support_id"],
            "support_url": row["support_url"] or "",
            "title": row["title"],
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
            text("SELECT id, title, content_md, status, published_at, embedding FROM kbd_entry WHERE id = :id"),
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

    # 2. 生成 embedding（事务外调用，避免长时间占用连接）
    embedding_generated = False
    embedding_vector: list[float] | None = None
    if _embedding_service:
        try:
            embedding_vector = await _embedding_service.embed_single(content_md)
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
                    embedding = :embedding::vector,
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
# SOP 文档审核接口（sop_document + sop_chunk 表）
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
    chunks_embedded: int = Field(..., description="成功生成 embedding 的分块数")
    published_at: str | None = Field(None, description="发布时间")


@sop_router.post("/{document_id}/approve", response_model=SopApproveResponse)
async def approve_sop_document(request: Request, document_id: int, body: SopApproveRequest):
    """审核通过 SOP 文档

    功能清单：
    1. 更新 sop_document.status → published
    2. 触发 sop_chunk embedding 生成（遍历所有 chunks）
    3. 生成 sop_chunk.tsv（BM25 索引，使用 to_tsvector('simple', content)）
    4. 设置 published_at = NOW()
    5. 记录 reviewer_id

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

    chunks_embedded = 0

    async with _db_manager.async_session_factory() as session:
        # 1. 查询 sop_document 是否存在
        result = await session.execute(select(SopDocument).where(SopDocument.id == document_id))
        sop_doc = result.scalar_one_or_none()

        if not sop_doc:
            raise HTTPException(status_code=404, detail=f"SOP 文档 {document_id} 不存在")

        current_status = sop_doc.status
        if current_status == "published":
            # 已发布，检查 chunks 的 embedding 状态
            chunk_result = await session.execute(select(SopChunk).where(SopChunk.document_id == document_id))
            existing_chunks = chunk_result.scalars().all()
            embedded_count = sum(1 for c in existing_chunks if c.embedding is not None)
            return SopApproveResponse(
                success=True,
                document_id=document_id,
                status="published",
                chunks_embedded=embedded_count,
                published_at=sop_doc.published_at.isoformat() if sop_doc.published_at else None,
            )

        # 2. 查询该文档的所有 chunks
        chunk_result = await session.execute(
            select(SopChunk).where(SopChunk.document_id == document_id).order_by(SopChunk.chunk_index)
        )
        chunks = chunk_result.scalars().all()

        if not chunks:
            logger.warning(
                event="sop_approve_no_chunks",
                document_id=document_id,
                message="SOP 文档没有分块，无法生成 embedding",
            )
            # 允许无 chunks 的文档发布（仅更新状态）

        # 3. 遍历 chunks 生成 embedding 和 tsv
        now = datetime.now(UTC)

        for chunk in chunks:
            if not chunk.content:
                logger.warning(
                    event="sop_chunk_empty_content",
                    document_id=document_id,
                    chunk_id=chunk.id,
                    chunk_index=chunk.chunk_index,
                    message="分块内容为空，跳过 embedding 生成",
                )
                continue

            # 生成 embedding（调用 embedding 服务）
            embedding_vector: list[float] | None = None
            if _embedding_service:
                try:
                    embedding_vector = await _embedding_service.embed_single(chunk.content)

                    # 检查向量维度
                    # sop_chunk.embedding 定义为 vector(1536)
                    expected_dim = 1536
                    actual_dim = len(embedding_vector)
                    if actual_dim != expected_dim:
                        logger.warning(
                            event="sop_chunk_embedding_dim_mismatch",
                            document_id=document_id,
                            chunk_id=chunk.id,
                            expected_dim=expected_dim,
                            actual_dim=actual_dim,
                            message=f"向量维度不匹配（期望 {expected_dim}，实际 {actual_dim}）",
                        )
                        # 维度不匹配时跳过 embedding 写入，避免数据库报错
                        embedding_vector = None

                    logger.info(
                        event="sop_chunk_embedding_generated",
                        document_id=document_id,
                        chunk_id=chunk.id,
                        chunk_index=chunk.chunk_index,
                        vector_dim=actual_dim,
                    )
                except Exception as exc:
                    logger.warning(
                        event="sop_chunk_embedding_failed",
                        document_id=document_id,
                        chunk_id=chunk.id,
                        chunk_index=chunk.chunk_index,
                        error=str(exc),
                        message="embedding 生成失败，将继续处理其他分块",
                    )
                    # 单个 chunk 失败不阻断整体流程

            # 更新 chunk 的 embedding 和 tsv（使用原生 SQL）
            if embedding_vector:
                vector_str = "[" + ",".join(str(v) for v in embedding_vector) + "]"
                await session.execute(
                    text(
                        """
                        UPDATE sop_chunk
                        SET embedding = :embedding::vector,
                            tsv = to_tsvector('simple', COALESCE(:chapter_title, '') || ' ' || COALESCE(:content, ''))
                        WHERE id = :chunk_id
                        """
                    ),
                    {
                        "chunk_id": chunk.id,
                        "embedding": vector_str,
                        "chapter_title": chunk.chapter_title or "",
                        "content": chunk.content,
                    },
                )
                chunks_embedded += 1
            else:
                # 仅生成 tsv（无 embedding）
                await session.execute(
                    text(
                        """
                        UPDATE sop_chunk
                        SET tsv = to_tsvector('simple', COALESCE(:chapter_title, '') || ' ' || COALESCE(:content, ''))
                        WHERE id = :chunk_id
                        """
                    ),
                    {
                        "chunk_id": chunk.id,
                        "chapter_title": chunk.chapter_title or "",
                        "content": chunk.content,
                    },
                )

        # 4. 更新 sop_document 状态
        sop_doc.status = "published"
        sop_doc.published_at = now
        sop_doc.reviewer_id = body.reviewer_id
        sop_doc.reviewed_at = now
        if body.review_note:
            # 使用原生 SQL 更新 review_note（避免 SQLAlchemy 的 None 处理）
            await session.execute(
                text("UPDATE sop_document SET review_note = :note WHERE id = :id"),
                {"id": document_id, "note": body.review_note},
            )

        await session.commit()

    logger.info(
        event="sop_approved",
        document_id=document_id,
        reviewer_id=body.reviewer_id,
        chunks_embedded=chunks_embedded,
        total_chunks=len(chunks),
    )

    return SopApproveResponse(
        success=True,
        document_id=document_id,
        status="published",
        chunks_embedded=chunks_embedded,
        published_at=now.isoformat(),
    )
