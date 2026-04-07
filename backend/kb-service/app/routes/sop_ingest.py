"""
KB Service — SOP 文档入库路由

POST /api/sop/ingest
  - 调用方：scripts/kbd ETL 脚本（SOP .docx 解析后写入）
  - 鉴权：INTERNAL_API_TOKEN（简单 Bearer Token）
  - 幂等：相同 docx_hash 的文档不会重复入库
  - 分块：按 Markdown 章节（## 或 ###）自动分块

功能清单：
1. 写入 sop_document 表
2. 解析 content_md 按章节分块
3. 写入 sop_chunk 表（每个章节一个 chunk）
4. 状态默认为 draft
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field
from shared.utils.logger import get_logger
from sqlalchemy import select

from app.models.sop_chunk import SopChunk
from app.models.sop_document import SopDocument

if TYPE_CHECKING:
    from shared.database.postgres import DatabaseManager

logger = get_logger("kb-service-sop-ingest")
router = APIRouter(prefix="/api/sop", tags=["sop-ingest"])

# 由 main.py 的 set_dependencies 注入
_db_manager: DatabaseManager | None = None


def set_dependencies(db: DatabaseManager) -> None:
    """注入数据库依赖"""
    global _db_manager
    _db_manager = db


def _check_auth(request: Request) -> None:
    """验证内部服务 Token（Bearer Token）"""
    from app.config import settings

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="缺少 Bearer Token")
    token = auth_header.split(" ", 1)[1]
    if token != settings.INTERNAL_API_TOKEN:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token 无效")


# ---- 请求/响应模型 ----


class SopIngestRequest(BaseModel):
    """SOP 文档入库请求"""

    source_id: str | None = Field(None, max_length=100, description="来源标识（可选，用于幂等）")
    title: str = Field(..., min_length=1, max_length=500, description="SOP 标题")
    content_md: str = Field(..., min_length=10, description="完整 Markdown 文档")
    category_id: str | None = Field(None, max_length=32, description="分类编码（可选）")
    docx_hash: str | None = Field(None, max_length=64, description="源文件 SHA256 哈希（幂等去重）")


class SopIngestResponse(BaseModel):
    """SOP 文档入库响应"""

    success: bool = Field(..., description="操作是否成功")
    document_id: int = Field(..., description="文档 ID")
    chunks_created: int = Field(..., description="创建的分块数")
    status: str = Field(..., description="文档状态")


# ---- 章节分块逻辑 ----


def split_by_chapters(content_md: str) -> list[tuple[str, str]]:
    """按 Markdown 章节（## 或 ###）分割内容

    Args:
        content_md: Markdown 全文

    Returns:
        list of (chapter_title, content) tuples
        每个元素包含章节标题和该章节的内容（到下一个标题之前）
    """
    # 匹配 ## 或 ### 标题行
    # 正则：捕获标题级别和标题文本
    pattern = r"^(#{2,3})\s+(.+)$"

    lines = content_md.split("\n")
    chunks: list[tuple[str, str]] = []

    current_title = "概述"  # 默认标题（文档开头部分）
    current_content_lines: list[str] = []
    found_first_heading = False

    for line in lines:
        match = re.match(pattern, line)
        if match:
            # 遇到新标题，保存前一个章节
            if current_content_lines:
                content = "\n".join(current_content_lines).strip()
                if content:
                    chunks.append((current_title, content))

            # 开始新章节
            current_title = match.group(2).strip()
            current_content_lines = [line]  # 包含标题行本身
            found_first_heading = True
        else:
            current_content_lines.append(line)

    # 保存最后一个章节
    if current_content_lines:
        content = "\n".join(current_content_lines).strip()
        if content:
            chunks.append((current_title, content))

    # 如果没有找到任何标题，整个文档作为一个 chunk
    if not found_first_heading and chunks:
        # 已经在上面处理了，无需额外操作
        pass

    return chunks


# ---- 路由 ----


@router.post("/ingest", status_code=status.HTTP_201_CREATED, response_model=SopIngestResponse)
async def ingest_sop_document(request: Request, body: SopIngestRequest):
    """SOP 文档入库

    将 SOP Markdown 文档按章节分块，写入 sop_document 和 sop_chunk 表。
    支持幂等（相同 docx_hash 不重复入库）。

    调用方：scripts/kbd ETL 脚本
    """
    _check_auth(request)

    if _db_manager is None:
        raise HTTPException(status_code=503, detail="服务未就绪")

    logger.info(
        event="sop_ingest_request",
        title=body.title[:50],
        source_id=body.source_id,
        docx_hash=body.docx_hash,
        content_length=len(body.content_md),
    )

    async with _db_manager.async_session_factory() as session:
        # 1. 幂等检查：docx_hash 去重
        if body.docx_hash:
            result = await session.execute(
                select(SopDocument).where(SopDocument.docx_hash == body.docx_hash)
            )
            existing_doc = result.scalar_one_or_none()
            if existing_doc:
                logger.info(
                    event="sop_ingest_duplicate",
                    docx_hash=body.docx_hash,
                    existing_id=existing_doc.id,
                    message="文档已存在，跳过入库",
                )
                # 返回已存在的文档信息
                chunk_count = await session.execute(
                    select(SopChunk).where(SopChunk.document_id == existing_doc.id)
                )
                chunks_created = len(chunk_count.scalars().all())
                return SopIngestResponse(
                    success=True,
                    document_id=existing_doc.id,
                    chunks_created=chunks_created,
                    status=existing_doc.status,
                )

        # 2. source_id 幂等检查（如果提供）
        if body.source_id:
            result = await session.execute(
                select(SopDocument).where(SopDocument.source_id == body.source_id)
            )
            existing_by_source = result.scalar_one_or_none()
            if existing_by_source:
                logger.info(
                    event="sop_ingest_duplicate_source",
                    source_id=body.source_id,
                    existing_id=existing_by_source.id,
                    message="source_id 已存在，跳过入库",
                )
                chunk_count = await session.execute(
                    select(SopChunk).where(SopChunk.document_id == existing_by_source.id)
                )
                chunks_created = len(chunk_count.scalars().all())
                return SopIngestResponse(
                    success=True,
                    document_id=existing_by_source.id,
                    chunks_created=chunks_created,
                    status=existing_by_source.status,
                )

        # 3. 创建 sop_document
        sop_doc = SopDocument(
            source_id=body.source_id,
            title=body.title,
            content_md=body.content_md,
            category_id=body.category_id,
            docx_hash=body.docx_hash,
            status="draft",
        )
        session.add(sop_doc)
        await session.flush()  # 获取生成的 ID

        document_id = sop_doc.id
        logger.info(
            event="sop_document_created",
            document_id=document_id,
            title=body.title[:50],
        )

        # 4. 按章节分块
        chapters = split_by_chapters(body.content_md)
        chunks_created = 0

        for idx, (chapter_title, content) in enumerate(chapters):
            chunk = SopChunk(
                document_id=document_id,
                chunk_index=idx,
                chapter_title=chapter_title[:200] if chapter_title else None,  # 限制长度
                content=content,
            )
            session.add(chunk)
            chunks_created += 1

        await session.commit()

    logger.info(
        event="sop_ingest_completed",
        document_id=document_id,
        chunks_created=chunks_created,
        title=body.title[:50],
    )

    return SopIngestResponse(
        success=True,
        document_id=document_id,
        chunks_created=chunks_created,
        status="draft",
    )
