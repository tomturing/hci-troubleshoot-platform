"""
诊断结论路由 — Diagnostic Item 管理 API

提供诊断结论的 CRUD 接口：
  - POST /api/conversations/{id}/diagnostic-items: 创建诊断条目（单个或批量）
  - PUT /api/conversations/{id}/diagnostic-items/{item_id}/status: 更新状态
  - PUT /api/conversations/{id}/diagnostic-items/archive: 归档所有条目
  - GET /api/conversations/{id}/diagnostic-items: 查询诊断条目

设计依据：
  - docs/task/agent/events/2026-05-26-SOP执行引擎-M1数据库与M2导航工具化.md T-AGT-19

鉴权：
  - 使用 INTERNAL_API_TOKEN（内部服务调用，agent-service → conversation-service）
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from shared.database.postgres import DatabaseManager
from shared.observability.logger import get_logger
from shared.observability.otel import get_current_trace_id

from ..repositories.diagnostic_item_repository import DiagnosticItemRepository

logger = get_logger("diagnostic-item-routes")
router = APIRouter(prefix="/api/conversations", tags=["diagnostic-items"])

# 由 main.py 注入
_db_manager: DatabaseManager | None = None


def set_dependencies(db: DatabaseManager) -> None:
    """注入数据库依赖"""
    global _db_manager
    _db_manager = db


def _check_auth(request: Request) -> None:
    """验证内部服务 Token"""
    from ..config import settings

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="缺少 Bearer Token")
    token = auth_header.split(" ", 1)[1]
    if token != settings.INTERNAL_API_TOKEN:
        raise HTTPException(status_code=401, detail="Token 无效")


class DiagnosticItemCreate(BaseModel):
    """单个诊断条目创建请求"""

    model_config = {"extra": "forbid"}  # 禁止额外字段，确保 Union 匹配正确

    stage: str = Field(..., description="阶段标识（S2/S3/S4/S5）")
    type: str = Field(..., description="类型（hypothesis/verification_step/root_cause/solution）")
    seq: int = Field(default=1, ge=1, description="序号（从1开始）")
    content: dict[str, Any] = Field(default_factory=dict, description="结构化内容")
    probability: float | None = Field(None, ge=0.0, le=1.0, description="假设概率（仅 hypothesis）")
    status: str = Field(default="pending", description="状态（pending/in_progress/confirmed/rejected）")


class DiagnosticItemBatchCreate(BaseModel):
    """批量创建诊断条目请求（S2 假设列表）"""

    model_config = {"extra": "forbid"}  # 禁止额外字段，确保 Union 匹配正确

    stage: str = Field(..., description="阶段标识")
    type: str = Field(..., description="类型")
    items: list[dict[str, Any]] = Field(..., min_length=1, description="条目数据列表")


class DiagnosticItemResponse(BaseModel):
    """诊断条目响应"""

    ok: bool = Field(..., description="操作是否成功")
    id: str | None = Field(None, description="条目 ID（单个创建时返回）")
    ids: list[str] | None = Field(None, description="条目 ID列表（批量创建时返回）")
    count: int | None = Field(None, description="创建条目数量")
    message: str = Field(..., description="结果消息")


class DiagnosticItemStatusUpdate(BaseModel):
    """状态更新请求"""

    status: str = Field(..., description="新状态（in_progress/confirmed/rejected/skipped）")
    content_update: dict[str, Any] | None = Field(None, description="内容更新（可选）")


class DiagnosticItemListResponse(BaseModel):
    """诊断条目列表响应"""

    items: list[dict[str, Any]] = Field(..., description="条目列表")
    total: int = Field(..., description="总数")


@router.post("/{conversation_id}/diagnostic-items", response_model=DiagnosticItemResponse)
async def create_diagnostic_item(
    request: Request,
    conversation_id: uuid.UUID,
    body: DiagnosticItemCreate | DiagnosticItemBatchCreate,
):
    """创建诊断条目（单个或批量）

    操作：
      - 单个创建：插入一条 diagnostic_item 记录
      - 批量创建：插入多条记录（S2 假设列表场景）

    Args:
        conversation_id: 会话 ID
        body: 创建请求（单个或批量）

    Returns:
        创建结果（条目 ID、数量）
    """
    _check_auth(request)

    if _db_manager is None:
        raise HTTPException(status_code=503, detail="服务未就绪")

    trace_id = get_current_trace_id()

    async with _db_manager.async_session_factory() as session:
        repo = DiagnosticItemRepository(session)

        # 判断是单个创建还是批量创建
        if isinstance(body, DiagnosticItemBatchCreate):
            # 批量创建
            logger.info(
                event="diagnostic_item_batch_create_request",
                conversation_id=str(conversation_id),
                stage=body.stage,
                type=body.type,
                count=len(body.items),
                trace_id=trace_id,
            )

            created_items = await repo.batch_create(
                conversation_id=conversation_id,
                stage=body.stage,
                type=body.type,
                items_data=body.items,
                trace_id=trace_id,
            )

            await session.commit()

            logger.info(
                event="diagnostic_item_batch_create_success",
                conversation_id=str(conversation_id),
                count=len(created_items),
                trace_id=trace_id,
            )

            return DiagnosticItemResponse(
                ok=True,
                ids=[str(item.id) for item in created_items],
                count=len(created_items),
                message=f"已创建 {len(created_items)} 条诊断条目",
            )

        else:
            # 单个创建
            logger.info(
                event="diagnostic_item_create_request",
                conversation_id=str(conversation_id),
                stage=body.stage,
                type=body.type,
                seq=body.seq,
                trace_id=trace_id,
            )

            item = await repo.create(
                conversation_id=conversation_id,
                stage=body.stage,
                type=body.type,
                seq=body.seq,
                content=body.content,
                probability=body.probability,
                status=body.status,
                trace_id=trace_id,
            )

            await session.commit()

            logger.info(
                event="diagnostic_item_create_success",
                conversation_id=str(conversation_id),
                item_id=str(item.id),
                stage=body.stage,
                type=body.type,
                trace_id=trace_id,
            )

            return DiagnosticItemResponse(
                ok=True,
                id=str(item.id),
                count=1,
                message="诊断条目已创建",
            )


@router.put("/{conversation_id}/diagnostic-items/{item_id}/status", response_model=DiagnosticItemResponse)
async def update_diagnostic_item_status(
    request: Request,
    conversation_id: uuid.UUID,
    item_id: uuid.UUID,
    body: DiagnosticItemStatusUpdate,
):
    """更新诊断条目状态

    操作：
      - 更新 status 字段（in_progress/confirmed/rejected/skipped）
      - 可选更新 content 字段

    Args:
        conversation_id: 会话 ID
        item_id: 条目 ID
        body: 状态更新请求

    Returns:
        更新结果
    """
    _check_auth(request)

    if _db_manager is None:
        raise HTTPException(status_code=503, detail="服务未就绪")

    trace_id = get_current_trace_id()

    logger.info(
        event="diagnostic_item_status_update_request",
        conversation_id=str(conversation_id),
        item_id=str(item_id),
        new_status=body.status,
        trace_id=trace_id,
    )

    async with _db_manager.async_session_factory() as session:
        repo = DiagnosticItemRepository(session)

        item = await repo.update_status(
            item_id=item_id,
            new_status=body.status,
            content_update=body.content_update,
        )

        if item is None:
            raise HTTPException(status_code=404, detail="诊断条目不存在")

        await session.commit()

        logger.info(
            event="diagnostic_item_status_update_success",
            conversation_id=str(conversation_id),
            item_id=str(item_id),
            status=body.status,
            trace_id=trace_id,
        )

        return DiagnosticItemResponse(
            ok=True,
            id=str(item_id),
            message=f"状态已更新为 {body.status}",
        )


@router.put("/{conversation_id}/diagnostic-items/archive", response_model=DiagnosticItemResponse)
async def archive_diagnostic_items(
    request: Request,
    conversation_id: uuid.UUID,
):
    """归档会话的所有诊断条目（S6 用户选 B 重进 S1）

    操作：
      - 批量更新所有非 archived 状态的条目为 archived

    Args:
        conversation_id: 会话 ID

    Returns:
        归档结果（更新的条目数量）
    """
    _check_auth(request)

    if _db_manager is None:
        raise HTTPException(status_code=503, detail="服务未就绪")

    trace_id = get_current_trace_id()

    logger.info(
        event="diagnostic_item_archive_request",
        conversation_id=str(conversation_id),
        trace_id=trace_id,
    )

    async with _db_manager.async_session_factory() as session:
        repo = DiagnosticItemRepository(session)

        count = await repo.archive_all(conversation_id)

        await session.commit()

        logger.info(
            event="diagnostic_item_archive_success",
            conversation_id=str(conversation_id),
            count=count,
            trace_id=trace_id,
        )

        return DiagnosticItemResponse(
            ok=True,
            count=count,
            message=f"已归档 {count} 条诊断条目",
        )


@router.get("/{conversation_id}/diagnostic-items", response_model=DiagnosticItemListResponse)
async def get_diagnostic_items(
    request: Request,
    conversation_id: uuid.UUID,
    stage: str | None = None,
    type: str | None = None,
    status: str | None = None,
):
    """查询会话的诊断条目（可按阶段/类型/状态过滤）

    Args:
        conversation_id: 会话 ID
        stage: 阶段过滤（可选）
        type: 类型过滤（可选）
        status: 状态过滤（可选）

    Returns:
        诊断条目列表
    """
    _check_auth(request)

    if _db_manager is None:
        raise HTTPException(status_code=503, detail="服务未就绪")

    trace_id = get_current_trace_id()

    async with _db_manager.async_session_factory() as session:
        repo = DiagnosticItemRepository(session)

        items = await repo.get_by_conversation(
            conversation_id=conversation_id,
            stage=stage,
            type=type,
            status=status,
        )

        logger.info(
            event="diagnostic_items_retrieved",
            conversation_id=str(conversation_id),
            count=len(items),
            filters={"stage": stage, "type": type, "status": status},
            trace_id=trace_id,
        )

        # 转换为字典列表
        items_dict = [
            {
                "id": str(item.id),
                "conversation_id": str(item.conversation_id),
                "stage": item.stage,
                "type": item.type,
                "seq": item.seq,
                "content": item.content,
                "probability": item.probability,
                "status": item.status,
                "created_at": item.created_at.isoformat() if item.created_at else None,
                "updated_at": item.updated_at.isoformat() if item.updated_at else None,
            }
            for item in items
        ]

        return DiagnosticItemListResponse(
            items=items_dict,
            total=len(items),
        )
