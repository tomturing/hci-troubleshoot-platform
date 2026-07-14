"""
KB Service — 文档入库路由

POST /api/kb/sop/import
  - 调用方：管理员（手动导入 SOP 技能节点）
  - 批量写入 kb_sop_node

POST /api/kbd/ingest
  - 调用方：data-pipeline/kbd/ 数据流水线
  - 写入 kbd_entry 表（深信服案例原始数据）
  - 幂等：support_id 唯一性校验
  - 状态默认为 draft，审核通过后才生成 embedding
"""

from __future__ import annotations

import base64
import io
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field
from shared.observability.logger import get_logger
from sqlalchemy import select

from app.models.kbd_entry import KbdEntry, KbdImage, strip_markdown

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


@router.post("/sop/import", status_code=status.HTTP_201_CREATED)
async def import_sop_nodes(request: Request, body: SopImportRequest):
    """批量导入 SOP 节点到 kb_sop_node

    调用方：管理员手动导入、data-pipeline/kbd ETL 脚本
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

    用于 data-pipeline/kbd/ 数据流水线调用，将深信服案例写入 kbd_entry 表。

    字段设计（与 8 大标准章节对齐）：
    - 章节字段（叙述类）：由 pipeline 从案例 HTML 自动提取，Markdown 格式
    - signals_json：关键信号集合（pipeline 写入空列表，抽取阶段/审核期填充）
    - content_md：由 pipeline 传入（含截图视觉描述），admin 编辑后重建时可省略

    幂等控制：
    - 默认幂等：已存在记录跳过，避免重复入库
    - override=true：强制覆盖已存在记录
    - override_status：状态过滤，防止误覆盖已发布数据
        - 不传 = 默认 ['draft']（安全覆盖）
        - ['all'] = 所有状态（谨慎使用）
        - ['draft', 'published'] = 仅指定状态
    """

    support_id: str = Field(..., min_length=1, max_length=20, description="深信服案例ID（幂等键）")

    title: str = Field(..., min_length=1, description="案例标题")

    # 8 大标准章节（叙述字段，Markdown 格式）
    problem_description: str = Field("", description="问题描述（## 问题描述 章节 Markdown）")
    alert_info: str = Field("", description="告警信息（## 告警信息 章节 Markdown）")
    steps_text: str = Field("", description="有效排查步骤（自然语言 Markdown，供人阅读）")
    root_cause: str = Field("", description="根因（## 根因 章节 Markdown）")
    solution: str = Field("", description="解决方案（## 解决方案 章节 Markdown）")
    operational_impact: str = Field("", description="操作影响范围（可选章节 Markdown）")
    is_temporary: str = Field("", description="是否是临时解决方案（可选章节 Markdown）")
    recommendations: str = Field("", description="建议与总结（可选章节 Markdown）")

    # 关键信号集合（pipeline 写入空列表，抽取阶段/审核期填充后 agent 可见）
    signals_json: list[dict] = Field(
        default_factory=list,
        description=(
            "关键信号集合（producer/consumer，供 agent 执行与判定）；"
            "格式：[{id,signal_category,keyword,acquirer,acquirer_args,produces,requires,matcher}]；"
            "pipeline 写入空列表，由关键信号分级抽取阶段填充"
        ),
    )

    # 图片视觉描述列表（pipeline 从 Vision LLM 缓存写入；admin 只读）
    images_json: list[dict] = Field(
        default_factory=list,
        description=(
            "图片视觉描述列表；格式：[{seq, section, desc}]；"
            "seq: 跨章节全局序号（从0开始）；"
            "section: 归属章节字段名；desc: Vision LLM 生成的描述"
        ),
    )
    # 图片二进制（base64）：IMPORT 阶段原子写入 kbd_image 表，消灭 upload_images 孤儿脚本
    # pipeline 零直连 DB；desc 初始空，由 VISION 阶段 reanalyze 填充
    images: list[dict] = Field(
        default_factory=list,
        description=(
            "图片二进制列表；格式：[{seq, section, mime_type, data_base64}]；"
            "data_base64: 原始图片 base64 编码；"
            "与 images_json 的 seq 一一对应，原子写入 kbd_image 表"
        ),
    )

    # 聚合渲染（含视觉描述，pipeline 写入；admin 编辑章节后服务端自动重建）
    content_md: str | None = Field(None, description="聚合渲染 Markdown（含截图视觉描述）；不传时由章节字段自动组装")
    content_raw: str | None = Field(
        None, description="聚合纯文本（不含 Markdown 格式与截图说明）；不传时由 content_md 自动提取"
    )

    metadata: dict = Field(default_factory=dict, description="JSONB 补充字段（来源元信息等）")
    ai_category_id: str | None = Field(None, max_length=32, description="AI 分类建议 ID")
    ai_category_conf: float | None = Field(None, ge=0.0, le=1.0, description="分类置信度")
    ai_category_reason: str | None = Field(None, description="分类理由")

    # 覆盖控制
    override: bool = Field(
        False,
        description="强制覆盖已存在的记录。false=幂等跳过；true=根据 override_status 覆盖",
    )
    override_status: list[str] | None = Field(
        None,
        description=("仅覆盖指定状态的记录。不传=默认['draft']；['all']=所有状态；['draft','published']=仅指定状态"),
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


# KBD 入库接口常量
DEFAULT_OVERRIDE_STATUS = ["draft"]
ALL_STATUS_MARKER = ["all", "*"]


def _validate_images_contract(images: list[dict], images_json: list[dict]) -> None:
    """契约校验：images 的 seq 集合 ⊆ images_json 的 seq 集合。

    约束：每个图片二进制必须对应 images_json 中的一条占位记录。
    - images_json 中可能有 desc 为空（待 VISION 填充）的占位条目
    - 但每个 images 元素（带 data_base64）的 seq 必须已在 images_json 中声明

    一致性由 converter.py 保证：
    - images_json 与 images 使用相同的 seq 编号
    - desc 初始空，由 VISION 阶段（reanalyze）填充

    Raises:
        ValueError: 契约违反（防呆，提醒调用方修复）
    """
    if not images:
        return
    img_seqs = {item.get("seq") for item in images if item.get("seq") is not None}
    json_seqs = {item.get("seq") for item in images_json if item.get("seq") is not None}
    extra = img_seqs - json_seqs
    if extra:
        raise ValueError(
            f"images/images_json 契约违反：images 包含未在 images_json 声明的 seq={extra}。"
            f"每个图片二进制必须对应 images_json 中的一条记录（desc 可空）。"
        )


async def _persist_kbd_images(session, kbd_entry_id: int, images: list[dict]) -> int:
    """将请求中的 images（base64）upsert 到 kbd_image 表（按 kbd_entry_id+seq）。

    IMPORT 阶段原子写入图片二进制，消灭 upload_images_to_db 孤儿脚本。
    存原始二进制（reanalyze 时按需压缩），与 upload_images_to_db 行为一致。

    Returns:
        成功写入的图片数
    """
    if not images:
        return 0

    result = await session.execute(
        select(KbdImage).where(KbdImage.kbd_entry_id == kbd_entry_id)
    )
    existing_map: dict[int, KbdImage] = {img.seq: img for img in result.scalars().all()}

    written = 0
    for img in images:
        seq = img.get("seq")
        if seq is None:
            continue
        mime_type = img.get("mime_type", "image/png")
        try:
            image_data = base64.b64decode(img["data_base64"])
        except Exception as exc:
            logger.warning(
                event="kbd_image_decode_failed",
                kbd_entry_id=kbd_entry_id, seq=seq, error=str(exc),
            )
            continue
        width = height = None
        try:
            from PIL import Image
            with Image.open(io.BytesIO(image_data)) as im:
                width, height = im.size
        except Exception:
            pass
        if seq in existing_map:
            row = existing_map[seq]
            row.image_data = image_data
            row.mime_type = mime_type
            row.width = width
            row.height = height
        else:
            session.add(KbdImage(
                kbd_entry_id=kbd_entry_id, seq=seq,
                image_data=image_data, mime_type=mime_type,
                width=width, height=height,
            ))
        written += 1
    return written


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

    调用方：data-pipeline/kbd/ 数据流水线

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

    # P2 契约校验：images 与 images_json 的 seq 一致性
    try:
        _validate_images_contract(body.images, body.images_json)
    except ValueError as exc:
        logger.error(event="kbd_ingest_contract_violation", error=str(exc))
        raise HTTPException(status_code=422, detail=str(exc))

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
        content_length=len(body.content_md or ""),
        override=body.override,
        override_status=body.override_status,
        allowed_statuses=allowed_statuses,
    )

    async with _db_manager.async_session_factory() as session:
        # 1. 幂等性校验：检查 support_id 是否已存在
        existing_result = await session.execute(select(KbdEntry).where(KbdEntry.support_id == body.support_id))
        existing_entry = existing_result.scalar_one_or_none()

        if existing_entry:
            # 已存在记录的处理逻辑
            existing_status = existing_entry.status

            if body.override:
                # 检查状态是否允许覆盖
                status_allowed = (
                    allowed_statuses is None  # 无限制
                    or existing_status in allowed_statuses
                )

                if status_allowed:
                    # 执行覆盖（结构化章节字段）
                    existing_entry.title = body.title
                    existing_entry.problem_description = body.problem_description
                    existing_entry.alert_info = body.alert_info
                    existing_entry.steps_text = body.steps_text
                    existing_entry.root_cause = body.root_cause
                    existing_entry.solution = body.solution
                    existing_entry.operational_impact = body.operational_impact
                    existing_entry.is_temporary = body.is_temporary
                    existing_entry.recommendations = body.recommendations
                    existing_entry.signals_json = body.signals_json
                    # P2-5 修复：先保存旧 images_json（含已识别的 desc），再覆盖。
                    # 否则「先清空 images_json 再 rebuild_content_md()」会用空 desc 重建，
                    # 导致截图描述丢失且依赖 VISION 随后必跑才恢复（不幂等）。
                    old_images_json = list(existing_entry.images_json or [])
                    existing_entry.images_json = body.images_json
                    # content_md：优先用传入值（含视觉描述），否则以旧 desc 回填后从章节重建
                    existing_entry.content_md = body.content_md or existing_entry.rebuild_content_md(old_images_json=old_images_json)
                    existing_entry.content_raw = body.content_raw or strip_markdown(existing_entry.content_md)
                    existing_entry.entry_metadata = body.metadata
                    if body.ai_category_id:
                        existing_entry.ai_category_id = body.ai_category_id
                    if body.ai_category_conf is not None:
                        existing_entry.ai_category_conf = body.ai_category_conf
                    if body.ai_category_reason:
                        existing_entry.ai_category_reason = body.ai_category_reason
                    await _persist_kbd_images(session, existing_entry.id, body.images)
                    await session.commit()

                    logger.info(
                        event="kbd_ingest_overridden",
                        support_id=body.support_id,
                        kbd_id=existing_entry.id,
                        status=existing_status,
                        has_signals_json=len(body.signals_json) > 0,
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

        # 2. 创建新条目（结构化章节字段）
        temp_content_md = body.content_md
        if not temp_content_md:
            # 临时生成一个 kbd 实例用于重建 content_md
            temp_entry = KbdEntry(
                problem_description=body.problem_description,
                alert_info=body.alert_info,
                steps_text=body.steps_text,
                root_cause=body.root_cause,
                solution=body.solution,
                operational_impact=body.operational_impact,
                is_temporary=body.is_temporary,
                recommendations=body.recommendations,
                images_json=body.images_json,
            )
            temp_content_md = temp_entry.rebuild_content_md()

        new_entry = KbdEntry(
            support_id=body.support_id,
            title=body.title,
            problem_description=body.problem_description,
            alert_info=body.alert_info,
            steps_text=body.steps_text,
            root_cause=body.root_cause,
            solution=body.solution,
            operational_impact=body.operational_impact,
            is_temporary=body.is_temporary,
            recommendations=body.recommendations,
            signals_json=body.signals_json,
            images_json=body.images_json,
            # content_md：优先用传入值（含视觉描述），否则从章节重建
            content_md=temp_content_md,
            content_raw=body.content_raw or strip_markdown(temp_content_md),
            entry_metadata=body.metadata,
            ai_category_id=body.ai_category_id,
            ai_category_conf=body.ai_category_conf,
            ai_category_reason=body.ai_category_reason,
            status="draft",
        )
        session.add(new_entry)
        await session.flush()  # 拿 new_entry.id，与 kbd_image 同事务原子写入
        await _persist_kbd_images(session, new_entry.id, body.images)
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
