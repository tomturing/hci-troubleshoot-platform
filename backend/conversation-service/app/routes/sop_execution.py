"""
SOP 执行路由 — SOP 执行状态管理 API

提供 SOP 执行实例的推进和管理接口：
  - POST /api/conversations/{id}/sop/advance: 推进到下一节点（advance_sop 工具调用）

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


@router.post("/{conversation_id}/sop/advance", response_model=SopAdvanceResponse)
async def advance_sop_execution(
    request: Request,
    conversation_id: uuid.UUID,
    body: SopAdvanceRequest,
):
    """推进 SOP 执行到下一节点（agent-service 的 advance_sop 工具调用）。

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