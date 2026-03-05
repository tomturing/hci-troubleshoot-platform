"""
KB Service — 管理后台路由

提供文档状态机管理接口（审核/发布/下线）和文档列表查询。
仅供管理员使用，需 INTERNAL_API_TOKEN 鉴权。

GET  /api/kb/documents            — 查询文档列表（分页 + 状态过滤）
GET  /api/kb/documents/{id}       — 查询单个文档详情
PATCH /api/kb/documents/{id}      — 更新文档状态（审核通过/发布/归档）
DELETE /api/kb/documents/{id}     — 删除文档（级联删除 chunks）
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel
from shared.utils.logger import get_logger
from sqlalchemy import select

from app.models.document import KBDocument

if TYPE_CHECKING:
    from shared.database.postgres import DatabaseManager

logger = get_logger("kb-service-admin")
router = APIRouter(prefix="/api/kb", tags=["admin"])

_db_manager: "DatabaseManager | None" = None


def set_dependencies(db: "DatabaseManager") -> None:
    global _db_manager
    _db_manager = db


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
