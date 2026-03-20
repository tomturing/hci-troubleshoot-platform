"""
KB Service — 文档入库路由

POST /api/kb/ingest
  - 调用方：LearningClaw（AI 学习成果写入）、ETL 脚本（批量历史案例导入）
  - 鉴权：INTERNAL_API_TOKEN（简单 Bearer Token）
  - 幂等：相同 source_id + content_hash 的文档不会重复入库

POST /api/kb/sop/import
  - 调用方：管理员（手动导入 SOP 技能节点）
  - 批量写入 kb_sop_node
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field
from shared.utils.logger import get_logger

from app.services.ingestor import IngestorService

if TYPE_CHECKING:
    from shared.database.postgres import DatabaseManager

    from app.services.embedding import EmbeddingService

logger = get_logger("kb-service-ingest")
router = APIRouter(prefix="/api/kb", tags=["ingest"])

# 由 main.py 的 set_dependencies 注入
_db_manager: DatabaseManager | None = None
_embedding_service: EmbeddingService | None = None


def set_dependencies(db: DatabaseManager, embedding: EmbeddingService) -> None:
    global _db_manager, _embedding_service
    _db_manager = db
    _embedding_service = embedding


# ---- 请求/响应模型 ----

class IngestRequest(BaseModel):
    """文档入库请求"""

    title: str = Field(..., min_length=1, max_length=500, description="文档标题")
    content_md: str = Field(..., min_length=10, description="Markdown 全文")
    source_id: str | None = Field(None, max_length=50, description="原始案例ID（可选，用于幂等）")
    source_type: str = Field("kb", pattern="^(kb|sop|realtime)$", description="来源类型")
    category_l1: str | None = Field(None, max_length=100, description="一级分类")
    category_l2: str | None = Field(None, max_length=100, description="二级分类")
    tags: list[str] = Field(default_factory=list, description="标签列表")
    summary: str | None = Field(None, description="摘要（中文）")
    judgment_logic: str | None = Field(None, description="排查逻辑（中文）")
    yaml_meta: dict | None = Field(None, description="结构化元数据")
    difficulty: int = Field(3, ge=1, le=5, description="难度 1-5")
    verified_version: str | None = Field(None, max_length=50, description="已验证的产品版本")


class SopNodeImportItem(BaseModel):
    """单个 SOP 节点导入项"""

    skill_id: str = Field(..., max_length=100)
    node_name: str = Field(..., max_length=200)
    parent_id: int | None = None
    keywords: list[str] = Field(..., min_length=1)
    file_path: str | None = None
    content: str | None = None
    level: int = Field(1, ge=1, le=2)
    sort_order: int = 0


class SopImportRequest(BaseModel):
    """批量 SOP 节点导入请求"""

    nodes: list[SopNodeImportItem] = Field(..., min_length=1)


def _check_auth(request: Request) -> None:
    """验证内部服务 Token（Bearer Token）"""
    from app.config import settings

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="缺少 Bearer Token")
    token = auth_header.split(" ", 1)[1]
    if token != settings.INTERNAL_API_TOKEN:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token 无效")


# ---- 路由 ----

@router.post("/ingest", status_code=status.HTTP_201_CREATED)
async def ingest_document(request: Request, body: IngestRequest):
    """文档入库

    将 Markdown 文档分块、embedding，写入知识库。
    支持幂等（相同内容不重复入库）。

    调用方：LearningClaw / data-pipeline ETL 脚本
    """
    _check_auth(request)

    if _db_manager is None or _embedding_service is None:
        raise HTTPException(status_code=503, detail="服务未就绪")

    logger.info(
        event="ingest_request",
        title=body.title[:50],
        source_id=body.source_id,
        source_type=body.source_type,
        content_length=len(body.content_md),
    )

    try:
        service = IngestorService(_db_manager, _embedding_service)
        result = await service.ingest(
            title=body.title,
            content_md=body.content_md,
            source_id=body.source_id,
            source_type=body.source_type,
            category_l1=body.category_l1,
            category_l2=body.category_l2,
            tags=body.tags,
            summary=body.summary,
            judgment_logic=body.judgment_logic,
            yaml_meta=body.yaml_meta,
            difficulty=body.difficulty,
            verified_version=body.verified_version,
        )
        return result.to_dict()
    except Exception as exc:
        logger.error(event="ingest_failed", error=str(exc), title=body.title[:50])
        raise HTTPException(status_code=500, detail=f"入库失败: {exc}") from exc


@router.post("/sop/import", status_code=status.HTTP_201_CREATED)
async def import_sop_nodes(request: Request, body: SopImportRequest):
    """批量导入 SOP 节点到 kb_sop_node

    调用方：管理员手动导入、data-pipeline ETL 脚本
    """
    _check_auth(request)

    if _db_manager is None:
        raise HTTPException(status_code=503, detail="数据库未就绪")


    from app.models.sop_node import KBSopNode

    logger.info(event="sop_import_request", node_count=len(body.nodes))

    async with _db_manager.async_session_factory() as session:
        created = 0
        for node in body.nodes:
            sop_node = KBSopNode(
                skill_id=node.skill_id,
                node_name=node.node_name,
                parent_id=node.parent_id,
                keywords=node.keywords,
                file_path=node.file_path,
                content=node.content,
                level=node.level,
                sort_order=node.sort_order,
            )
            session.add(sop_node)
            created += 1
        await session.commit()

    logger.info(event="sop_import_completed", created=created)
    return {"created": created, "total": len(body.nodes)}
