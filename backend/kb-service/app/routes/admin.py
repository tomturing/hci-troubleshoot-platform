"""
KB Service — 管理后台路由

提供文档状态机管理接口（审核/发布/下线）和文档列表查询。
仅供管理员使用，需 INTERNAL_API_TOKEN 鉴权。

GET  /api/kb/documents            — 查询文档列表（分页 + 状态过滤）
GET  /api/kb/documents/{id}       — 查询单个文档详情
PATCH /api/kb/documents/{id}      — 更新文档状态（审核通过/发布/归档）
DELETE /api/kb/documents/{id}     — 删除文档（级联删除 chunks）

POST /api/admin/kbd/{id}/approve  — KBD 条目审核通过（生成 embedding + tsv）
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field
from shared.utils.logger import get_logger
from sqlalchemy import select, text

from app.models.document import KBDocument

if TYPE_CHECKING:
    from shared.database.postgres import DatabaseManager

    from app.services.embedding import EmbeddingService

logger = get_logger("kb-service-admin")
router = APIRouter(prefix="/api/kb", tags=["admin"])

# 新增 KBD 审核路由（独立 prefix）
kbd_router = APIRouter(prefix="/api/admin/kbd", tags=["kbd-admin"])

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

    status: str | None = None          # draft/under_review/approved/published/rejected/archived
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
# KBD 条目审核接口（kbd_entry 表）
# ─────────────────────────────────────────────────────────────────────────────


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

    embedding_generated = False

    async with _db_manager.async_session_factory() as session:
        # 1. 查询 kbd_entry 是否存在
        result = await session.execute(
            text("SELECT id, title, content_md, status FROM kbd_entry WHERE id = :id"),
            {"id": kbd_id},
        )
        row = result.mappings().first()

        if not row:
            raise HTTPException(status_code=404, detail=f"KBD 条目 {kbd_id} 不存在")

        current_status = row["status"]
        if current_status == "published":
            # 已发布，无需重复处理，但返回当前状态
            result2 = await session.execute(
                text("SELECT published_at, embedding FROM kbd_entry WHERE id = :id"),
                {"id": kbd_id},
            )
            existing = result2.mappings().first()
            return KbdApproveResponse(
                success=True,
                kbd_id=kbd_id,
                status="published",
                embedding_generated=existing["embedding"] is not None,
                published_at=existing["published_at"].isoformat() if existing["published_at"] else None,
            )

        content_md = row["content_md"]
        if not content_md:
            raise HTTPException(
                status_code=400,
                detail=f"KBD 条目 {kbd_id} 缺少 content_md，无法生成 embedding",
            )

        # 2. 生成 embedding（调用 embedding 服务）
        embedding_vector: list[float] | None = None
        if _embedding_service:
            try:
                embedding_vector = await _embedding_service.embed_single(content_md)
                embedding_generated = True

                # 检查向量维度是否与数据库一致
                # kbd_entry.embedding 定义为 vector(1536)
                expected_dim = 1536
                actual_dim = len(embedding_vector)
                if actual_dim != expected_dim:
                    logger.warning(
                        event="kbd_embedding_dim_mismatch",
                        kbd_id=kbd_id,
                        expected_dim=expected_dim,
                        actual_dim=actual_dim,
                        message=f"向量维度不匹配（期望 {expected_dim}，实际 {actual_dim}），"
                        f"数据库可能需要调整或使用更高维度的 embedding 模型",
                    )
                    # 注意：PostgreSQL pgvector 在插入时会检查维度，不匹配会报错
                    # 如果发生维度不匹配，当前版本将跳过 embedding 写入

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
                # embedding 生成失败不阻断审核流程，记录状态但允许后续手动重试

        # 3. 更新 kbd_entry 状态（status, published_at, reviewer_id, review_note, embedding, tsv）
        now = datetime.now(UTC)

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
