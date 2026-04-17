"""
KB Service — 文档入库路由

POST /api/kb/ingest
  - 调用方：LearningClaw（AI 学习成果写入）、ETL 脚本（批量历史案例导入）
  - 鉴权：INTERNAL_API_TOKEN（简单 Bearer Token）
  - 幂等：相同 source_id + content_hash 的文档不会重复入库

POST /api/kb/sop/import
  - 调用方：管理员（手动导入 SOP 技能节点）
  - 批量写入 kb_sop_node

POST /api/kbd/ingest
  - 调用方：scripts/kbd/ 数据流水线
  - 写入 kbd_entry 表（深信服案例原始数据）
  - 幂等：support_id 唯一性校验
  - 状态默认为 draft，审核通过后才生成 embedding
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field
from shared.utils.logger import get_logger
from sqlalchemy import select

from app.models.kbd_entry import KbdEntry
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

    调用方：LearningClaw / scripts/kbd ETL 脚本
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

    调用方：管理员手动导入、scripts/kbd ETL 脚本
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


# ─────────────────────────────────────────────────────────────────────────────
# KBD 条目入库接口（kbd_entry 表）
# ─────────────────────────────────────────────────────────────────────────────


class KbdIngestRequest(BaseModel):
    """KBD 条目入库请求

    用于 scripts/kbd/ 数据流水线调用，将深信服案例写入 kbd_entry 表。

    参数设计（遵循第一性原理）：
    - 默认幂等：已存在记录跳过，避免重复入库
    - override=true：强制覆盖已存在记录
    - override_status：状态过滤，防止误覆盖已发布数据
        - 不传 = 默认 ['draft']（安全覆盖）
        - ['all'] = 所有状态（谨慎使用）
        - ['draft', 'published'] = 仅指定状态
    """

    support_id: str = Field(..., min_length=1, max_length=20, description="深信服案例ID（幂等键）")
    support_url: str | None = Field(None, description="原始案例 URL")
    title: str = Field(..., min_length=1, description="案例标题")
    content_md: str = Field(..., min_length=1, description="结构化 Markdown 内容")
    metadata: dict = Field(default_factory=dict, description="JSONB 补充字段")
    ai_category_id: str | None = Field(None, max_length=32, description="AI 分类建议 ID")
    ai_category_conf: float | None = Field(None, ge=0.0, le=1.0, description="分类置信度")
    ai_category_reason: str | None = Field(None, description="分类理由")

    # 新参数：覆盖控制
    override: bool = Field(
        False,
        description="强制覆盖已存在的记录。false=幂等跳过；true=根据 override_status 覆盖",
    )
    override_status: list[str] | None = Field(
        None,
        description=(
            "仅覆盖指定状态的记录。"
            "不传=默认['draft']；['all']=所有状态；['draft','published']=仅指定状态"
        ),
    )

    # 向后兼容（deprecated）
    force_update: bool = Field(
        False,
        description="[已废弃] 请使用 override 参数。仅更新 draft 状态的记录",
    )


class KbdIngestResponse(BaseModel):
    """KBD 条目入库响应"""

    success: bool = Field(..., description="操作是否成功")
    kbd_id: int = Field(..., description="KBD 条目 ID")
    status: str = Field(..., description="当前状态")
    action: str | None = Field(
        None,
        description="执行动作：created / skipped / overridden",
    )
    message: str | None = Field(None, description="附加消息（如幂等提示）")


@router.post("/kbd/ingest", status_code=status.HTTP_201_CREATED)
async def ingest_kbd_entry(request: Request, body: KbdIngestRequest):
    """KBD 条目入库

    功能说明：
    1. 写入 kbd_entry 表（深信服案例原始数据）
    2. support_id 幂等性校验（已存在则根据 override 决定行为）
    3. 状态默认为 draft（审核通过后才生成 embedding）
    4. 不生成 embedding（审核通过时由 approve_kbd_entry 触发）

    参数行为矩阵：
    | override | override_status | 记录状态 | 结果 |
    |----------|-----------------|---------|------|
    | false    | -               | 不存在  | ✅ created |
    | false    | -               | 已存在  | ⏭️ skipped |
    | true     | 不传（默认draft）| draft   | ✅ overridden |
    | true     | 不传（默认draft）| published | ⏭️ skipped（状态不匹配）|
    | true     | ['all']         | draft   | ✅ overridden |
    | true     | ['all']         | published | ✅ overridden（谨慎使用）|
    | true     | ['draft','published'] | draft | ✅ overridden |

    调用方：scripts/kbd/ 数据流水线

    响应体示例：
    ```json
    // 新建成功
    {
      "success": true,
      "kbd_id": 123,
      "status": "draft",
      "action": "created"
    }

    // 幂等跳过
    {
      "success": true,
      "kbd_id": 123,
      "status": "draft",
      "action": "skipped",
      "message": "记录已存在，幂等跳过"
    }

    // 覆盖成功
    {
      "success": true,
      "kbd_id": 123,
      "status": "draft",
      "action": "overridden",
      "message": "记录已覆盖"
    }

    // 状态不匹配跳过
    {
      "success": true,
      "kbd_id": 123,
      "status": "published",
      "action": "skipped",
      "message": "记录状态 'published' 不在 override_status 范围内"
    }
    ```
    """
    _check_auth(request)

    if _db_manager is None:
        raise HTTPException(status_code=503, detail="数据库未就绪")

    # 参数解析：向后兼容 force_update → override
    override = body.override or body.force_update
    # 默认状态过滤：['draft']
    DEFAULT_OVERRIDE_STATUS = ["draft"]
    ALL_STATUS_MARKER = ["all", "*"]

    if body.override_status is None:
        # 不传 override_status = 默认仅 draft
        allowed_statuses = DEFAULT_OVERRIDE_STATUS
    elif any(s in ALL_STATUS_MARKER for s in body.override_status):
        # ['all'] 或 ['*'] = 无限制（所有状态）
        allowed_statuses = None  # None 表示"所有状态"
    else:
        # 用户指定的状态列表
        allowed_statuses = body.override_status

    logger.info(
        event="kbd_ingest_request",
        support_id=body.support_id,
        title=body.title[:50],
        content_length=len(body.content_md),
        override=override,
        override_status=body.override_status,
        allowed_statuses=allowed_statuses,
    )

    async with _db_manager.async_session_factory() as session:
        # 1. 幂等性校验：检查 support_id 是否已存在
        existing_result = await session.execute(
            select(KbdEntry).where(KbdEntry.support_id == body.support_id)
        )
        existing_entry = existing_result.scalar_one_or_none()

        if existing_entry:
            # 已存在记录的处理逻辑
            existing_status = existing_entry.status

            if override:
                # 检查状态是否允许覆盖
                status_allowed = (
                    allowed_statuses is None  # 无限制
                    or existing_status in allowed_statuses
                )

                if status_allowed:
                    # 执行覆盖
                    existing_entry.title = body.title
                    existing_entry.content_md = body.content_md
                    existing_entry.entry_metadata = body.metadata
                    if body.ai_category_id:
                        existing_entry.ai_category_id = body.ai_category_id
                    if body.ai_category_conf:
                        existing_entry.ai_category_conf = body.ai_category_conf
                    if body.ai_category_reason:
                        existing_entry.ai_category_reason = body.ai_category_reason
                    await session.commit()

                    logger.info(
                        event="kbd_ingest_overridden",
                        support_id=body.support_id,
                        kbd_id=existing_entry.id,
                        status=existing_status,
                        content_length=len(body.content_md),
                    )

                    return KbdIngestResponse(
                        success=True,
                        kbd_id=existing_entry.id,
                        status=existing_status,
                        action="overridden",
                        message="记录已覆盖",
                    )
                else:
                    # 状态不匹配，跳过覆盖
                    logger.info(
                        event="kbd_ingest_status_mismatch",
                        support_id=body.support_id,
                        kbd_id=existing_entry.id,
                        existing_status=existing_status,
                        allowed_statuses=allowed_statuses,
                    )

                    return KbdIngestResponse(
                        success=True,
                        kbd_id=existing_entry.id,
                        status=existing_status,
                        action="skipped",
                        message=f"记录状态 '{existing_status}' 不在 override_status 范围内",
                    )
            else:
                # 幂等跳过（不覆盖）
                logger.info(
                    event="kbd_ingest_idempotent",
                    support_id=body.support_id,
                    kbd_id=existing_entry.id,
                    status=existing_status,
                )

                return KbdIngestResponse(
                    success=True,
                    kbd_id=existing_entry.id,
                    status=existing_status,
                    action="skipped",
                    message="记录已存在，幂等跳过",
                )

        # 2. 创建新条目
        new_entry = KbdEntry(
            support_id=body.support_id,
            support_url=body.support_url,
            title=body.title,
            content_md=body.content_md,
            entry_metadata=body.metadata,
            ai_category_id=body.ai_category_id,
            ai_category_conf=body.ai_category_conf,
            ai_category_reason=body.ai_category_reason,
            status="draft",
        )
        session.add(new_entry)
        await session.commit()

        # 3. 刷新获取 ID
        await session.refresh(new_entry)

    logger.info(
        event="kbd_ingest_created",
        support_id=body.support_id,
        kbd_id=new_entry.id,
        title=body.title[:50],
    )

    return KbdIngestResponse(
        success=True,
        kbd_id=new_entry.id,
        status=new_entry.status,
        action="created",
    )
