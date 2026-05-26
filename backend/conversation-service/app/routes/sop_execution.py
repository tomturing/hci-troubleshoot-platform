"""
SOP 执行路由 — SOP 执行状态管理 API

提供 SOP 执行实例的推进和管理接口：
  - POST /api/conversations/{id}/sop/advance: 推进到下一节点（sop_advance 工具调用）

设计依据：
  - docs/task/agent/events/2026-05-26-SOP执行引擎-M1数据库与M2导航工具化.md T-AGT-21

鉴权：
  - 使用 INTERNAL_API_TOKEN（内部服务调用，agent-service → conversation-service）
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from shared.database.postgres import DatabaseManager
from shared.observability.logger import get_logger
from shared.observability.otel import get_current_trace_id

from ..repositories.sop_execution_repository import SopExecutionRepository

logger = get_logger("sop-execution-routes")
router = APIRouter(prefix="/api/conversations", tags=["sop-execution"])

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


class SopCreateRequest(BaseModel):
    """SOP 执行实例创建请求"""

    sop_document_id: int = Field(..., description="SOP 文档 ID")
    root_node_id: str = Field(default="n-1", description="根节点 ID（默认 n-1）")


class SopCreateResponse(BaseModel):
    """SOP 执行实例创建响应"""

    ok: bool = Field(..., description="创建是否成功")
    conversation_id: str = Field(..., description="会话 ID")
    sop_document_id: int = Field(..., description="SOP 文档 ID")
    current_node_id: str = Field(..., description="当前节点 ID（根节点）")
    status: str = Field(..., description="执行状态（active）")
    message: str = Field(..., description="创建结果消息")


class SopAdvanceRequest(BaseModel):
    """SOP 推进请求"""

    target_node_id: str = Field(..., min_length=1, description="目标节点 ID")
    reasoning: str = Field(..., min_length=1, description="LLM 推进理由")
    node_type: str | None = Field(None, description="目标节点类型（branch/diagnosis/solution）")
    variables_extracted: dict[str, Any] | None = Field(None, description="变量池更新")


class SopAdvanceResponse(BaseModel):
    """SOP 推进响应"""

    ok: bool = Field(..., description="操作是否成功")
    current_node_id: str = Field(..., description="当前节点 ID")
    node_type: str | None = Field(None, description="节点类型")
    message: str = Field(..., description="推进结果消息")
    is_completed: bool = Field(False, description="SOP 是否已完成（到达叶节点）")


@router.post("/{conversation_id}/sop/create", response_model=SopCreateResponse)
async def sop_create_execution(
    request: Request,
    conversation_id: uuid.UUID,
    body: SopCreateRequest,
):
    """创建 SOP 执行实例（S1 阶段命中 SOP 时）。

    操作：
      1. 创建新的 sop_execution 记录
      2. 初始化 current_node_id 为根节点
      3. 记录 execution_log 首条（node_entered）

    Args:
        conversation_id: 会话 ID
        body: 创建请求（SOP 文档 ID、根节点 ID）

    Returns:
        创建结果（会话 ID、SOP 文档 ID、当前节点 ID）
    """
    _check_auth(request)

    if _db_manager is None:
        raise HTTPException(status_code=503, detail="服务未就绪")

    trace_id = get_current_trace_id()
    logger.info(
        event="sop_create_request",
        conversation_id=str(conversation_id),
        sop_document_id=body.sop_document_id,
        root_node_id=body.root_node_id,
        trace_id=trace_id,
    )

    async with _db_manager.async_session_factory() as session:
        repo = SopExecutionRepository(session)

        # 检查是否已存在活跃的执行实例（中断恢复场景）
        existing = await repo.get_active_by_conversation(conversation_id)
        if existing:
            # 已存在活跃实例，返回现有记录（用于恢复）
            logger.info(
                event="sop_create_existing",
                conversation_id=str(conversation_id),
                existing_id=str(existing.id),
                current_node_id=existing.current_node_id,
                trace_id=trace_id,
            )
            await session.commit()
            return SopCreateResponse(
                ok=True,
                conversation_id=str(conversation_id),
                sop_document_id=existing.sop_document_id,
                current_node_id=existing.current_node_id,
                status=existing.status,
                message="SOP 执行实例已存在，继续执行",
            )

        # 创建新的执行实例
        execution = await repo.create(
            conversation_id=conversation_id,
            sop_document_id=body.sop_document_id,
            current_node_id=body.root_node_id,
            trace_id=trace_id,
        )

        await session.commit()

        logger.info(
            event="sop_create_success",
            conversation_id=str(conversation_id),
            execution_id=str(execution.id),
            sop_document_id=body.sop_document_id,
            current_node_id=execution.current_node_id,
            trace_id=trace_id,
        )

        return SopCreateResponse(
            ok=True,
            conversation_id=str(conversation_id),
            sop_document_id=body.sop_document_id,
            current_node_id=execution.current_node_id,
            status=execution.status,
            message="SOP 执行实例已创建",
        )


@router.post("/{conversation_id}/sop/advance", response_model=SopAdvanceResponse)
async def sop_advance_execution(
    request: Request,
    conversation_id: uuid.UUID,
    body: SopAdvanceRequest,
):
    """推进 SOP 执行到下一节点（agent-service 的 sop_advance 工具调用）。

    操作：
      1. 更新 current_node_id 为目标节点
      2. 追加 execution_log 条目（node_entered）
      3. 追加 completed_steps（前一节点标记完成）
      4. 若叶节点（solution）则更新 status=completed

    Args:
        conversation_id: 会话 ID
        body: 推进请求（目标节点 ID、推理理由、节点类型、变量）

    Returns:
        推进结果（当前节点 ID、节点类型、是否完成）
    """
    _check_auth(request)

    if _db_manager is None:
        raise HTTPException(status_code=503, detail="服务未就绪")

    trace_id = get_current_trace_id()
    logger.info(
        event="sop_advance_request",
        conversation_id=str(conversation_id),
        target_node_id=body.target_node_id,
        node_type=body.node_type,
        trace_id=trace_id,
    )

    async with _db_manager.async_session_factory() as session:
        repo = SopExecutionRepository(session)

        # 推进执行
        execution = await repo.advance(
            conversation_id=conversation_id,
            target_node_id=body.target_node_id,
            reasoning=body.reasoning,
            node_type=body.node_type,
            variables_extracted=body.variables_extracted,
        )

        if execution is None:
            raise HTTPException(
                status_code=404,
                detail="SOP 执行实例不存在或状态非 active",
            )

        await session.commit()

        is_completed = execution.status == "completed"
        message = f"已推进到节点 {body.target_node_id}"
        if is_completed:
            message = "SOP 执行完成，已到达叶节点"

        logger.info(
            event="sop_advance_success",
            conversation_id=str(conversation_id),
            current_node_id=execution.current_node_id,
            status=execution.status,
            is_completed=is_completed,
            trace_id=trace_id,
        )

        return SopAdvanceResponse(
            ok=True,
            current_node_id=execution.current_node_id,
            node_type=body.node_type,
            message=message,
            is_completed=is_completed,
        )


@router.get("/{conversation_id}/sop/execution")
async def get_sop_execution(
    request: Request,
    conversation_id: uuid.UUID,
):
    """获取 SOP 执行实例详情（用于中断恢复）"""
    _check_auth(request)

    if _db_manager is None:
        raise HTTPException(status_code=503, detail="服务未就绪")

    trace_id = get_current_trace_id()

    async with _db_manager.async_session_factory() as session:
        repo = SopExecutionRepository(session)
        execution = await repo.get_by_conversation(conversation_id)

        if execution is None:
            raise HTTPException(
                status_code=404,
                detail="SOP 执行实例不存在",
            )

        logger.info(
            event="sop_execution_retrieved",
            conversation_id=str(conversation_id),
            status=execution.status,
            current_node_id=execution.current_node_id,
            trace_id=trace_id,
        )

        return {
            "id": str(execution.id),
            "conversation_id": str(execution.conversation_id),
            "sop_document_id": execution.sop_document_id,
            "current_node_id": execution.current_node_id,
            "status": execution.status,
            "context_variables": execution.context_variables,
            "completed_steps": execution.completed_steps,
            "execution_log": execution.execution_log,
            "pending_variable_name": execution.pending_variable_name,
            "created_at": execution.created_at.isoformat() if execution.created_at else None,
            "updated_at": execution.updated_at.isoformat() if execution.updated_at else None,
        }