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

import json
import os
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field, model_validator
from shared.database.postgres import DatabaseManager
from shared.database.redis import RedisManager
from shared.observability.logger import get_logger
from shared.observability.otel import get_current_trace_id
from sqlalchemy import text

from ..config import settings

logger = get_logger("agent-exec-routes")
router = APIRouter(tags=["agent-exec"])

_COMMAND_SECRET_RE = re.compile(
    r"(?i)(password|passwd|token|secret|api[_-]?key)(\s*[=:]\s*|\s+)([^\s'\"]+|['\"][^'\"]*['\"])",
)
_EXEC_STATE_TTL_SECONDS = int(os.getenv("AGENT_EXEC_STATE_TTL_SECONDS", "180"))
_ARTIFACT_RETENTION_DAYS = int(os.getenv("BRIDGE_ARTIFACT_RETENTION_DAYS", "30"))


def _redact_command(command: str | None) -> str | None:
    """对 Artifact 中的命令做保守脱敏，避免凭据进入长期存储。"""
    if command is None:
        return None
    return _COMMAND_SECRET_RE.sub(lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", command)[:4096]


# 由 main.py 注入
_db_manager: DatabaseManager | None = None
_redis_manager: RedisManager | None = None
MAX_EXEC_RESULT_CHARS = 256 * 1024


def set_dependencies(db: DatabaseManager, redis: RedisManager) -> None:
    """注入数据库和 Redis 依赖"""
    global _db_manager, _redis_manager
    _db_manager = db
    _redis_manager = redis


async def _resolve_conversation_case_id(conversation_id: uuid.UUID) -> str | None:
    """使用独立、完整生命周期的 Session 查询会话工单，禁止手动拆用 yield 依赖。"""
    if _db_manager is None:
        return None
    async with _db_manager.async_session_factory() as session:
        result = await session.execute(
            text("SELECT case_id FROM conversation WHERE conversation_id = :conversation_id"),
            {"conversation_id": conversation_id},
        )
        return result.scalar_one_or_none()


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


class OutputFilterRequest(BaseModel):
    """只允许字面量行筛选；该协议不能表达命令、正则或脚本。"""

    source: Literal["stdout", "stderr"] = "stdout"
    include: list[str] = Field(default_factory=list, max_length=8)
    exclude: list[str] = Field(default_factory=list, max_length=8)
    include_mode: Literal["all", "any"] = "all"
    case_sensitive: bool = True

    @model_validator(mode="after")
    def validate_literals(self) -> OutputFilterRequest:
        values = [*self.include, *self.exclude]
        if not values:
            raise ValueError("output_filter 至少需要 include 或 exclude")
        if any(not value or len(value.encode("utf-8")) > 512 for value in values):
            raise ValueError("output_filter 条件必须为 1~512 字节的非空字面量")
        return self


class AgentExecRequest(BaseModel):
    """Agent 执行命令推送请求"""

    exec_id: str = Field(..., description="执行 ID（UUID）")
    tool_name: str | None = Field(None, description="工具名称")
    command: str = Field(..., min_length=1, description="待执行命令")
    container: str | None = Field(None, description="目标容器")
    original_command: str | None = Field(None, description="工具调用原始命令")
    built_command: str | None = Field(None, description="服务端拼装后的实际执行命令")
    reason: str = Field(..., min_length=1, description="执行原因")
    risk_level: int = Field(..., ge=1, le=3, description="风险等级（1-3）")
    node_ip: str | None = Field(None, description="目标节点 IP")
    # B2 修订：case_id 改为可选。调用方（acli_exec / bash_exec / qfk_exec）可能未透传工单 ID，
    # 缺失时由会话关联解析（对齐 conversations.py 的 B1 修复），避免空串透传至 terminal_bridge 触发 exec.session_missing。
    case_id: str | None = Field(None, description="工单 ID（缺失时由会话关联解析，对齐 B1 修复）")
    trace_id: str | None = Field(None, description="端到端链路 ID（透传至 terminal_bridge）")
    traceparent: str | None = Field(None, pattern=r"^00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$")
    tool_call_id: str | None = None
    timeout: int = Field(120, ge=1, le=300, description="命令最大执行时间（秒）")
    output_filters: list[OutputFilterRequest] = Field(
        default_factory=list,
        max_length=8,
        description="terminal_bridge 逐行安全筛选规格；不解释 shell，也不执行 grep/awk",
    )


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
      2. 写入 Redis：SET exec:{exec_id} <context> EX 180（覆盖 Bridge 执行和结果回传窗口）
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

    # ── 工单 ID 兜底解析（对齐 conversations.py 的 B1 修复）──
    # acli_exec / bash_exec / qfk_exec 等调用方可能未透传 case_id，
    # 此时从会话关联的真实工单 ID 解析，避免空串透传至 terminal_bridge 触发 exec.session_missing。
    # 这是 LLM 工具路径与 qfk 诊断路径的统一收敛点：两处缺口一次性修复。
    effective_case_id = body.case_id
    if not effective_case_id:
        try:
            effective_case_id = await _resolve_conversation_case_id(conversation_id)
        except Exception as _exc:
            effective_case_id = None
            logger.error(
                event="agent_exec_case_id_resolve_error",
                conversation_id=str(conversation_id),
                exec_id=body.exec_id,
                error=str(_exc),
                trace_id=trace_id,
            )
        if effective_case_id:
            logger.warning(
                event="agent_exec_case_id_resolved",
                conversation_id=str(conversation_id),
                exec_id=body.exec_id,
                resolved_case_id=effective_case_id,
                trace_id=trace_id,
            )
        else:
            logger.error(
                event="agent_exec_case_id_unresolved",
                conversation_id=str(conversation_id),
                exec_id=body.exec_id,
                message="会话未关联工单，case_id 为空，命令将无法路由到 terminal_bridge 会话",
                trace_id=trace_id,
            )

    logger.info(
        event="agent_exec_push_request",
        conversation_id=str(conversation_id),
        exec_id=body.exec_id,
        tool_name=body.tool_name,
        case_id=effective_case_id,
        container=body.container,
        command_preview=body.command[:50],
        risk_level=body.risk_level,
        trace_id=trace_id,
    )

    # 1. 写入 Redis pending 上下文，覆盖 Bridge 执行超时和上游结果回传余量。
    pending_context = {
        "exec_id": body.exec_id,
        "tool_name": body.tool_name,
        "command": body.built_command or body.command,
        "container": body.container,
        "node_ip": body.node_ip,
        "case_id": effective_case_id,
        "conversation_id": str(conversation_id),
        "trace_id": body.trace_id or trace_id,
        "traceparent": body.traceparent,
        "tool_call_id": body.tool_call_id or body.exec_id,
        "timeout": body.timeout,
        "output_filters": [item.model_dump() for item in body.output_filters],
    }
    await _redis_manager.set(
        f"exec:{body.exec_id}",
        json.dumps(pending_context, ensure_ascii=False),
        ex=max(_EXEC_STATE_TTL_SECONDS, body.timeout + 15),
    )

    # 2. 推送 SSE 事件到前端
    # 通过 app.state 获取 SSE 推送服务

    # 获取 app 实例（从 request.app）
    app = request.app
    sse_pusher = getattr(app.state, "sse_pusher", None)

    event_data = {
        "execId": body.exec_id,
        "toolName": body.tool_name,
        "command": body.command,
        "container": body.container,
        "originalCommand": body.original_command,
        "builtCommand": body.built_command or body.command,
        "reason": body.reason,
        "riskLevel": body.risk_level,
        "nodeIp": body.node_ip,
        "caseId": effective_case_id,
        "conversationId": str(conversation_id),
        "traceId": body.trace_id,
        "traceparent": body.traceparent,
        "toolCallId": body.tool_call_id or body.exec_id,
        "timeout": body.timeout,
        "outputFilters": [item.model_dump() for item in body.output_filters],
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
    output: str = Field(..., max_length=MAX_EXEC_RESULT_CHARS, description="命令输出")
    exit_code: int = Field(..., description="退出码（0=成功）")
    stdout: str | None = Field(default=None, max_length=MAX_EXEC_RESULT_CHARS, description="标准输出")
    stderr: str | None = Field(default=None, max_length=MAX_EXEC_RESULT_CHARS, description="标准错误")
    trace_id: str | None = None
    traceparent: str | None = Field(default=None, pattern=r"^00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$")
    stdout_bytes: int | None = Field(default=None, ge=0)
    stderr_bytes: int | None = Field(default=None, ge=0)
    stdout_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    stderr_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    duration_ms: int | None = Field(default=None, ge=0)
    timed_out: bool = False
    cancelled: bool = False
    error_type: str | None = None
    artifact_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def validate_physical_stream_budget(self) -> ExecResultRequest:
        if len(self.stdout or "") + len(self.stderr or "") > MAX_EXEC_RESULT_CHARS:
            raise ValueError("stdout/stderr 合计超过 256 KiB 安全上限")
        return self


class ExecResultResponse(BaseModel):
    """执行结果回传响应"""

    ok: bool = Field(..., description="回传是否成功")
    exec_id: str = Field(..., description="执行 ID")
    message: str = Field(..., description="回传结果消息")


def _effective_stream_bytes(reported_bytes: int | None, content: str | None) -> int:
    """保留 Bridge 上报的零值；字段被旧客户端省略时按实际 UTF-8 内容兜底。"""
    return reported_bytes if reported_bytes is not None else len((content or "").encode())


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
        if _db_manager is not None:
            async for session in _db_manager.get_session():
                existing = await session.execute(
                    text("SELECT artifact_id FROM bridge_execution_artifacts WHERE exec_id = :exec_id"),
                    {"exec_id": body.exec_id},
                )
                if existing.scalar_one_or_none() is not None:
                    logger.info(event="exec_result_duplicate_accepted", exec_id=body.exec_id, trace_id=trace_id)
                    return ExecResultResponse(ok=True, exec_id=body.exec_id, message="执行结果已幂等接收")
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

    try:
        pending_context = (
            json.loads(pending_value) if isinstance(pending_value, str) else json.loads(pending_value.decode())
        )
    except (TypeError, ValueError, AttributeError):
        pending_context = {}

    effective_trace_id = body.trace_id or trace_id or pending_context.get("trace_id")
    artifact_id = str(body.artifact_id or uuid.uuid5(uuid.NAMESPACE_URL, f"hci-terminal-artifact:{body.exec_id}"))
    effective_stdout_bytes = _effective_stream_bytes(body.stdout_bytes, body.stdout)
    effective_stderr_bytes = _effective_stream_bytes(body.stderr_bytes, body.stderr)

    if _db_manager is None:
        raise HTTPException(status_code=503, detail="数据库未就绪")

    async for session in _db_manager.get_session():
        await session.execute(text("DELETE FROM bridge_execution_artifacts WHERE expires_at < now()"))
        await session.execute(
            text(
                """
                INSERT INTO bridge_execution_artifacts
                    (artifact_id, exec_id, case_id, conversation_id, tool_name, trace_id,
                     node_ip, container, command_redacted, stdout, stderr, exit_code,
                     stdout_bytes, stderr_bytes, stdout_sha256, stderr_sha256,
                     stdout_truncated, stderr_truncated, duration_ms, timed_out, cancelled,
                     status, error_type, expires_at)
                VALUES
                    (:artifact_id, :exec_id, :case_id, :conversation_id, :tool_name, :trace_id,
                     :node_ip, :container, :command_redacted, :stdout, :stderr, :exit_code,
                     :stdout_bytes, :stderr_bytes, :stdout_sha256, :stderr_sha256,
                     :stdout_truncated, :stderr_truncated, :duration_ms, :timed_out, :cancelled,
                     :status, :error_type, :expires_at)
                ON CONFLICT (exec_id) DO UPDATE SET
                    trace_id = EXCLUDED.trace_id,
                    stdout = EXCLUDED.stdout,
                    stderr = EXCLUDED.stderr,
                    exit_code = EXCLUDED.exit_code,
                    stdout_bytes = EXCLUDED.stdout_bytes,
                    stderr_bytes = EXCLUDED.stderr_bytes,
                    stdout_sha256 = EXCLUDED.stdout_sha256,
                    stderr_sha256 = EXCLUDED.stderr_sha256,
                    stdout_truncated = EXCLUDED.stdout_truncated,
                    stderr_truncated = EXCLUDED.stderr_truncated,
                    duration_ms = EXCLUDED.duration_ms,
                    timed_out = EXCLUDED.timed_out,
                    cancelled = EXCLUDED.cancelled,
                    status = EXCLUDED.status,
                    error_type = EXCLUDED.error_type,
                    updated_at = now()
                """
            ),
            {
                "artifact_id": artifact_id,
                "exec_id": body.exec_id,
                "case_id": pending_context.get("case_id"),
                "conversation_id": str(conversation_id),
                "tool_name": pending_context.get("tool_name"),
                "trace_id": effective_trace_id,
                "node_ip": pending_context.get("node_ip"),
                "container": pending_context.get("container"),
                "command_redacted": _redact_command(pending_context.get("command")),
                "stdout": body.stdout,
                "stderr": body.stderr,
                "exit_code": body.exit_code,
                "stdout_bytes": effective_stdout_bytes,
                "stderr_bytes": effective_stderr_bytes,
                "stdout_sha256": body.stdout_sha256,
                "stderr_sha256": body.stderr_sha256,
                "stdout_truncated": body.stdout_truncated,
                "stderr_truncated": body.stderr_truncated,
                "duration_ms": body.duration_ms,
                "timed_out": body.timed_out,
                "cancelled": body.cancelled,
                "status": "success" if body.exit_code == 0 else "failed",
                "error_type": body.error_type,
                "expires_at": datetime.now(UTC) + timedelta(days=_ARTIFACT_RETENTION_DAYS),
            },
        )
        await session.commit()

    result_data = {
        "exec_id": body.exec_id,
        "output": body.output,
        "exit_code": body.exit_code,
        "stdout": body.stdout,
        "stderr": body.stderr,
        "conversation_id": str(conversation_id),
        "user_id": user_id,
        "trace_id": effective_trace_id,
        "traceparent": body.traceparent,
        "artifact_id": artifact_id,
        "stdout_bytes": effective_stdout_bytes,
        "stderr_bytes": effective_stderr_bytes,
        "stdout_sha256": body.stdout_sha256,
        "stderr_sha256": body.stderr_sha256,
        "stdout_truncated": body.stdout_truncated,
        "stderr_truncated": body.stderr_truncated,
        "duration_ms": body.duration_ms,
        "timed_out": body.timed_out,
        "cancelled": body.cancelled,
        "error_type": body.error_type,
    }
    result_key = f"exec_result:{body.exec_id}"

    # 使用 Redis list 作为队列（LPUSH 写入，agent-service 端用 RPOP 读取）
    await _redis_manager.client.lpush(result_key, json.dumps(result_data, ensure_ascii=False))

    # 结果保留 180 秒，覆盖 Agent 默认 150 秒等待窗口并允许短暂重试。
    await _redis_manager.expire(result_key, _EXEC_STATE_TTL_SECONDS)

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
