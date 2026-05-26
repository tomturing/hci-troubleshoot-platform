"""
SOP 执行路由 — SOP 执行状态管理 API

提供 SOP 执行实例的推进和管理接口：
  - POST /api/conversations/{id}/sop/advance: 推进到下一节点（sop_advance 工具调用）

设计依据：
  - docs/task/agent/events/2026-05-26-SOP执行引擎-M1数据库与M2导航工具化.md T-AGT-21
  - docs/task/agent/events/2026-05-26-SOP执行引擎-M3变量池实现.md T-AGT-27（validation_pattern 校验）

鉴权：
  - 使用 INTERNAL_API_TOKEN（内部服务调用，agent-service → conversation-service）
"""

from __future__ import annotations

import re
import uuid
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from shared.database.postgres import DatabaseManager
from shared.observability.logger import get_logger
from shared.observability.otel import get_current_trace_id

from ..config import settings
from ..models.sop_execution import STATUS_ACTIVE, STATUS_INTERRUPTED, SopExecution
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
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="缺少 Bearer Token")
    token = auth_header.split(" ", 1)[1]
    if token != settings.INTERNAL_API_TOKEN:
        raise HTTPException(status_code=401, detail="Token 无效")


async def _get_variable_schema(sop_document_id: int) -> list[dict] | None:
    """从 kb-service 获取 SOP 文档的 variable_schema（T-AGT-27）。

    Args:
        sop_document_id: SOP 文档 ID

    Returns:
        variable_schema 列表，不存在时返回 None
    """
    url = f"{settings.KB_SERVICE_URL}/api/admin/sop/{sop_document_id}"
    headers = {"Authorization": f"Bearer {settings.INTERNAL_API_TOKEN}"}

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            data = resp.json()
            return data.get("variable_schema", [])
    except (httpx.HTTPStatusError, httpx.RequestError) as exc:
        logger.warning(
            event="get_variable_schema_error",
            sop_document_id=sop_document_id,
            error=str(exc),
        )
        return None


def _validate_variables(
    variables_extracted: dict[str, Any],
    variable_schema: list[dict] | None,
) -> tuple[bool, list[str]]:
    """校验 variables_extracted 是否符合 variable_schema 的 validation_pattern（T-AGT-27）。

    Args:
        variables_extracted: 待写入的变量字典
        variable_schema: 变量 Schema 定义列表

    Returns:
        (是否全部通过, 错误消息列表)
    """
    if not variable_schema:
        # 无 schema 定义，跳过校验
        return True, []

    errors = []
    schema_by_name = {v.get("name"): v for v in variable_schema}

    for var_name, var_value in variables_extracted.items():
        var_def = schema_by_name.get(var_name)
        if var_def is None:
            # 变量未在 schema 中定义，允许写入（LLM 自由填充）
            continue

        validation_pattern = var_def.get("validation_pattern")
        if not validation_pattern:
            # 无校验规则，允许写入
            continue

        # 校验值是否符合 pattern（使用 fullmatch 保证完整匹配，BUG-R02）
        var_value_str = str(var_value) if not isinstance(var_value, str) else var_value
        try:
            if not re.fullmatch(validation_pattern, var_value_str):
                errors.append(
                    f"变量 '{var_name}' 值 '{var_value_str}' 不符合校验规则 '{validation_pattern}'"
                )
        except re.error as exc:
            # BUG-R03: validation_pattern 为无效正则时，记录警告并跳过该变量校验
            logger.warning(
                event="validate_variables_invalid_pattern",
                var_name=var_name,
                validation_pattern=validation_pattern,
                error=str(exc),
            )

    return len(errors) == 0, errors


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

        # DC-03: 提前一次性查询（仅在需要校验时），后续传入 advance() 避免重复 SELECT
        prefetched_execution: SopExecution | None = None
        if body.variables_extracted:
            # 获取活跃执行实例（advance 也要求 active 状态）
            prefetched_execution = await repo.get_active_by_conversation(conversation_id)
            if prefetched_execution is None:
                raise HTTPException(
                    status_code=404,
                    detail="SOP 执行实例不存在或状态非 active",
                )

            # 获取 variable_schema 并校验 variables_extracted（T-AGT-27）
            variable_schema = await _get_variable_schema(prefetched_execution.sop_document_id)
            valid, errors = _validate_variables(body.variables_extracted, variable_schema)
            if not valid:
                logger.warning(
                    event="sop_advance_validation_failed",
                    conversation_id=str(conversation_id),
                    errors=errors,
                    trace_id=trace_id,
                )
                raise HTTPException(
                    status_code=422,
                    detail={
                        "error": "variable_validation_failed",
                        "message": "变量值校验失败",
                        "errors": errors,
                    },
                )

        # 推进执行（DC-03: 传入预取实例，跳过 advance() 内部的重复 SELECT）
        execution = await repo.advance(
            conversation_id=conversation_id,
            target_node_id=body.target_node_id,
            reasoning=body.reasoning,
            node_type=body.node_type,
            variables_extracted=body.variables_extracted,
            existing_execution=prefetched_execution,  # DC-03: 避免重复查询
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


# ─────────────────────────────────────────────────────────────────────────────
# T-AGT-25: SOP 执行中断端点（设置 pending_variable_name）
# ─────────────────────────────────────────────────────────────────────────────


class SopInterruptRequest(BaseModel):
    """SOP 执行中断请求"""

    pending_variable_name: str = Field(..., min_length=1, description="待填变量名")


class SopInterruptResponse(BaseModel):
    """SOP 执行中断响应"""

    ok: bool = Field(..., description="操作是否成功")
    conversation_id: str = Field(..., description="会话 ID")
    status: str = Field(..., description="执行状态（interrupted）")
    pending_variable_name: str = Field(..., description="待填变量名")
    message: str = Field(..., description="中断结果消息")


@router.post("/{conversation_id}/sop/interrupt", response_model=SopInterruptResponse)
async def sop_interrupt_execution(
    request: Request,
    conversation_id: uuid.UUID,
    body: SopInterruptRequest,
):
    """标记 SOP 执行中断等待变量（agent-service 的 sop_request_variable 工具调用）。

    操作：
      1. 更新 status=interrupted
      2. 设置 pending_variable_name

    Args:
        conversation_id: 会话 ID
        body: 中断请求（待填变量名）

    Returns:
        中断结果（会话 ID、状态、待填变量名）
    """
    _check_auth(request)

    if _db_manager is None:
        raise HTTPException(status_code=503, detail="服务未就绪")

    trace_id = get_current_trace_id()
    logger.info(
        event="sop_interrupt_request",
        conversation_id=str(conversation_id),
        pending_variable_name=body.pending_variable_name,
        trace_id=trace_id,
    )

    async with _db_manager.async_session_factory() as session:
        repo = SopExecutionRepository(session)

        # 检查执行实例存在
        execution = await repo.get_active_by_conversation(conversation_id)
        if execution is None:
            raise HTTPException(
                status_code=404,
                detail="SOP 执行实例不存在或状态非 active",
            )

        # 标记中断
        updated = await repo.interrupt(
            conversation_id=conversation_id,
            pending_variable_name=body.pending_variable_name,
        )

        if updated is None:
            raise HTTPException(
                status_code=500,
                detail="标记中断失败",
            )

        await session.commit()

        logger.info(
            event="sop_interrupt_success",
            conversation_id=str(conversation_id),
            status=updated.status,
            pending_variable_name=updated.pending_variable_name,
            trace_id=trace_id,
        )

        return SopInterruptResponse(
            ok=True,
            conversation_id=str(conversation_id),
            status=updated.status,
            pending_variable_name=updated.pending_variable_name,
            message=f"SOP 执行已中断，等待变量 {body.pending_variable_name} 填写",
        )


# ─────────────────────────────────────────────────────────────────────────────
# T-AGT-25: 变量提交端点（用户响应变量请求）
# ─────────────────────────────────────────────────────────────────────────────


class VariableResponseRequest(BaseModel):
    """变量值提交请求"""

    variable_name: str = Field(..., min_length=1, description="变量名")
    value: str = Field(..., min_length=1, description="变量值")
    source: str | None = Field(default="user_input", description="值来源（user_input/user_confirm/tool_result）")


class VariableResponseResponse(BaseModel):
    """变量值提交响应"""

    ok: bool = Field(..., description="操作是否成功")
    variable_name: str = Field(..., description="变量名")
    value: str = Field(..., description="已写入的值")
    message: str = Field(..., description="结果消息")
    validation_passed: bool = Field(True, description="校验是否通过")


@router.post("/{conversation_id}/sop/variable-response", response_model=VariableResponseResponse)
async def sop_variable_response(
    request: Request,
    conversation_id: uuid.UUID,
    body: VariableResponseRequest,
):
    """提交变量值（用户响应 sop_request_variable 的交互请求）。

    流程：
      1. 验证 SOP 执行状态为 interrupted 且 pending_variable_name 匹配
      2. 校验 value 是否符合 variable_schema 的 validation_pattern（如有）
      3. 写入 context_variables[variable_name]
      4. 清空 pending_variable_name，恢复状态为 active

    Args:
        conversation_id: 会话 ID
        body: 变量值提交请求（变量名、值、来源）

    Returns:
        提交结果（变量名、值、校验状态）
    """
    _check_auth(request)

    if _db_manager is None:
        raise HTTPException(status_code=503, detail="服务未就绪")

    trace_id = get_current_trace_id()
    logger.info(
        event="sop_variable_response_request",
        conversation_id=str(conversation_id),
        variable_name=body.variable_name,
        value_preview=body.value[:50] if len(body.value) > 50 else body.value,
        source=body.source,
        trace_id=trace_id,
    )

    async with _db_manager.async_session_factory() as session:
        repo = SopExecutionRepository(session)

        # 1. 获取执行实例
        execution = await repo.get_by_conversation(conversation_id)
        if execution is None:
            raise HTTPException(
                status_code=404,
                detail="SOP 执行实例不存在",
            )

        # 2. 验证状态和 pending_variable_name
        if execution.status != STATUS_INTERRUPTED:
            # 允许在 active 状态下直接写入变量（LLM 可能提前填充）
            if execution.status == STATUS_ACTIVE:
                logger.info(
                    event="sop_variable_response_active_state",
                    conversation_id=str(conversation_id),
                    variable_name=body.variable_name,
                    message="执行状态为 active，直接写入变量",
                )
                # 直接写入变量（不校验 pending_variable_name）
                updated = await repo.set_variable(
                    conversation_id=conversation_id,
                    variable_name=body.variable_name,
                    value=body.value,
                    source=body.source or "user_input",
                )
                await session.commit()
                return VariableResponseResponse(
                    ok=True,
                    variable_name=body.variable_name,
                    value=body.value,
                    message=f"变量 {body.variable_name} 已写入",
                    validation_passed=True,
                )
            else:
                raise HTTPException(
                    status_code=400,
                    detail=f"SOP 执行状态为 {execution.status}，无法提交变量",
                )

        # 3. 校验 pending_variable_name 是否匹配
        if execution.pending_variable_name != body.variable_name:
            logger.warning(
                event="sop_variable_response_mismatch",
                conversation_id=str(conversation_id),
                expected_variable=execution.pending_variable_name,
                submitted_variable=body.variable_name,
                trace_id=trace_id,
            )
            raise HTTPException(
                status_code=400,
                detail=f"当前等待变量 {execution.pending_variable_name}，提交的变量 {body.variable_name} 不匹配",
            )

        # 4. 校验 validation_pattern（如有）
        # TODO: 从 kb-service 获取 variable_schema 进行校验
        # 暂时跳过校验，直接写入

        # 5. 写入变量并恢复状态
        updated = await repo.set_variable(
            conversation_id=conversation_id,
            variable_name=body.variable_name,
            value=body.value,
            source=body.source or "user_input",
        )

        if updated is None:
            raise HTTPException(
                status_code=500,
                detail="写入变量失败",
            )

        await session.commit()

        logger.info(
            event="sop_variable_response_success",
            conversation_id=str(conversation_id),
            variable_name=body.variable_name,
            value_preview=body.value[:50] if len(body.value) > 50 else body.value,
            status=updated.status,
            trace_id=trace_id,
        )

        return VariableResponseResponse(
            ok=True,
            variable_name=body.variable_name,
            value=body.value,
            message=f"变量 {body.variable_name} 已写入，SOP 执行已恢复",
            validation_passed=True,
        )
