"""
Agent 执行命令路由 — Agent 工具执行命令推送与结果回传

提供 Agent 工具执行的命令推送和结果回传接口：
  - POST /internal/conversations/{id}/agent-exec: 推送执行命令（agent-service → conversation-service）
  - POST /api/conversations/{id}/exec-result: 回传执行结果（前端 → conversation-service）

设计依据：
  - docs/task/agent/agent工具任务清单.md T-TOOL-05, T-TOOL-06, T-TOOL-07

鉴权：
  - /internal/* 接口使用 INTERNAL_API_TOKEN（内部服务调用）
  - /api/* 接口使用用户 Session Token（前端调用）
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field
from shared.database.postgres import DatabaseManager
from shared.database.redis import RedisManager
from shared.observability.logger import get_logger
from shared.observability.otel import get_current_trace_id

from ..config import settings

logger = get_logger("agent-exec-routes")
router = APIRouter(tags=["agent-exec"])

# 由 main.py 注入
_db_manager: DatabaseManager | None = None
_redis_manager: RedisManager | None = None


def set_dependencies(db: DatabaseManager, redis: RedisManager) -> None:
    """注入数据库和 Redis 依赖"""
    global _db_manager, _redis_manager
    _db_manager = db
    _redis_manager = redis


def _check_internal_auth(request: Request) -> None:
    """验证内部服务 Token（/internal/* 接口使用）"""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="缺少 Bearer Token")
    token = auth_header.split(" ", 1)[1]
    if token != settings.INTERNAL_API_TOKEN:
        raise HTTPException(status_code=401, detail="Token 无效")


def _check_user_session(authorization: str | None = Header(default=None)) -> str:
    """验证用户 Session Token（/api/* 接口使用）

    Args:
        authorization: Bearer Token 头

    Returns:
        用户 ID（从 Token 中提取）

    Raises:
        HTTPException: Token 无效或缺失
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="缺少 Bearer Token")

    token = authorization[7:].strip()
    # TODO: 实现用户 Session 验证（当前 MVP 使用简化验证）
    # 生产环境需调用 case-service 或 api-gateway 的 Session 验证接口
    if len(token) < 10:
        raise HTTPException(status_code=401, detail="Token 无效")

    # 返回临时用户 ID（后续替换为真实用户信息）
    return "user-placeholder"


# ─────────────────────────────────────────────────────────────────────────────
# T-TOOL-05: 推送执行命令（内部服务调用）
# ─────────────────────────────────────────────────────────────────────────────


class AgentExecRequest(BaseModel):
    """Agent 执行命令推送请求"""

    exec_id: str = Field(..., description="执行 ID（UUID）")
    command: str = Field(..., min_length=1, description="待执行命令")
    reason: str = Field(..., min_length=1, description="执行原因")
    risk_level: int = Field(..., ge=1, le=3, description="风险等级（1-3）")
    node_ip: str | None = Field(None, description="目标节点 IP")
    case_id: str = Field(..., description="工单 ID")


class AgentExecResponse(BaseModel):
    """Agent 执行命令推送响应"""

    ok: bool = Field(..., description="推送是否成功")
    exec_id: str = Field(..., description="执行 ID")
    message: str = Field(..., description="推送结果消息")


@router.post(
    "/internal/conversations/{conversation_id}/agent-exec",
    response_model=AgentExecResponse,
    status_code=202,
)
async def push_agent_exec_command(
    request: Request,
    conversation_id: uuid.UUID,
    body: AgentExecRequest,
):
    """推送 Agent 执行命令到前端（agent-service → conversation-service）。

    流程：
      1. 验证内部服务 Token
      2. 写入 Redis：SET exec:{exec_id} "pending" EX 120（120秒超时）
      3. 推送 SSE 事件 agent_exec_command 到前端
      4. 返回 202 Accepted

    Args:
        conversation_id: 会话 ID
        body: 执行命令请求（exec_id、command、risk_level、node_ip、case_id）

    Returns:
        推送结果（exec_id、状态）
    """
    _check_internal_auth(request)

    if _redis_manager is None or _redis_manager.client is None:
        raise HTTPException(status_code=503, detail="Redis 服务未就绪")

    trace_id = get_current_trace_id()
    logger.info(
        event="agent_exec_push_request",
        conversation_id=str(conversation_id),
        exec_id=body.exec_id,
        command_preview=body.command[:50],
        risk_level=body.risk_level,
        trace_id=trace_id,
    )

    # 1. 写入 Redis pending 状态（120秒超时）
    await _redis_manager.set(f"exec:{body.exec_id}", "pending", ex=120)

    # 2. 推送 SSE 事件到前端
    # 通过 app.state 获取 SSE 推送服务

    # 获取 app 实例（从 request.app）
    app = request.app
    sse_pusher = getattr(app.state, "sse_pusher", None)

    event_data = {
        "execId": body.exec_id,
        "command": body.command,
        "reason": body.reason,
        "riskLevel": body.risk_level,
        "nodeIp": body.node_ip,
        "caseId": body.case_id,
        "conversationId": str(conversation_id),
    }

    if sse_pusher:
        # 使用 SSE 推送服务发送事件
        await sse_pusher.push_event(
            conversation_id=str(conversation_id),
            event_type="agent_exec_command",
            data=event_data,
        )
        logger.info(
            event="agent_exec_sse_pushed",
            conversation_id=str(conversation_id),
            exec_id=body.exec_id,
            trace_id=trace_id,
        )
    else:
        # 兜底：直接记录日志（MVP 阶段 SSE 推送可能未初始化）
        logger.warning(
            event="agent_exec_sse_pusher_not_available",
            conversation_id=str(conversation_id),
            exec_id=body.exec_id,
            message="SSE 推送服务未初始化，事件未推送",
            trace_id=trace_id,
        )

    return AgentExecResponse(
        ok=True,
        exec_id=body.exec_id,
        message="执行命令已推送，等待前端响应",
    )


# ─────────────────────────────────────────────────────────────────────────────
# T-TOOL-06: 回传执行结果（前端调用）
# ─────────────────────────────────────────────────────────────────────────────


class ExecResultRequest(BaseModel):
    """执行结果回传请求"""

    exec_id: str = Field(..., description="执行 ID（UUID）")
    output: str = Field(..., description="命令输出")
    exit_code: int = Field(..., description="退出码（0=成功）")
    stdout: str | None = Field(default=None, description="标准输出")
    stderr: str | None = Field(default=None, description="标准错误")


class ExecResultResponse(BaseModel):
    """执行结果回传响应"""

    ok: bool = Field(..., description="回传是否成功")
    exec_id: str = Field(..., description="执行 ID")
    message: str = Field(..., description="回传结果消息")


@router.post(
    "/api/conversations/{conversation_id}/exec-result",
    response_model=ExecResultResponse,
)
async def submit_exec_result(
    conversation_id: uuid.UUID,
    body: ExecResultRequest,
    user_id: str = Depends(_check_user_session),
):
    """回传执行结果（前端 → conversation-service）。

    流程：
      1. 验证用户 Session Token
      2. 验证 exec_id 对应的 Redis key 存在
      3. 写入 Redis 队列：LPUSH exec_result:{exec_id} {json}
      4. 删除 pending key：DEL exec:{exec_id}
      5. 返回 200 OK

    Args:
        conversation_id: 会话 ID
        body: 执行结果（exec_id、output、exit_code）
        user_id: 用户 ID（从 Token 提取）

    Returns:
        回传结果（exec_id、状态）
    """
    if _redis_manager is None or _redis_manager.client is None:
        raise HTTPException(status_code=503, detail="Redis 服务未就绪")

    trace_id = get_current_trace_id()
    logger.info(
        event="exec_result_submit_request",
        conversation_id=str(conversation_id),
        exec_id=body.exec_id,
        exit_code=body.exit_code,
        user_id=user_id,
        trace_id=trace_id,
    )

    # 1. 验证 exec_id 对应的 Redis key 存在
    pending_key = f"exec:{body.exec_id}"
    pending_value = await _redis_manager.get(pending_key)

    if pending_value is None:
        logger.warning(
            event="exec_result_invalid_exec_id",
            conversation_id=str(conversation_id),
            exec_id=body.exec_id,
            message="exec_id 不存在或已过期",
            trace_id=trace_id,
        )
        raise HTTPException(
            status_code=400,
            detail=f"执行 ID {body.exec_id} 不存在或已过期",
        )

    # 2. 写入 Redis 队列（结果数据）
    import json

    result_data = {
        "exec_id": body.exec_id,
        "output": body.output,
        "exit_code": body.exit_code,
        "stdout": body.stdout,
        "stderr": body.stderr,
        "conversation_id": str(conversation_id),
        "user_id": user_id,
        "trace_id": trace_id,
    }
    result_key = f"exec_result:{body.exec_id}"

    # 使用 Redis list 作为队列（LPUSH 写入，agent-service 端用 RPOP 读取）
    await _redis_manager.client.lpush(result_key, json.dumps(result_data, ensure_ascii=False))

    # 设置队列过期时间（120秒，与 pending key 相同）
    await _redis_manager.expire(result_key, 120)

    # 3. 删除 pending key
    await _redis_manager.delete(pending_key)

    logger.info(
        event="exec_result_submitted",
        conversation_id=str(conversation_id),
        exec_id=body.exec_id,
        exit_code=body.exit_code,
        trace_id=trace_id,
    )

    return ExecResultResponse(
        ok=True,
        exec_id=body.exec_id,
        message="执行结果已回传",
    )
