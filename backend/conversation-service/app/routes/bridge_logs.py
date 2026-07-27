"""
terminal_bridge 日志回采接口 - 统一工单关联的日志落库

提供前端（Custom-UI）在收到 terminal_bridge 经 WebSocket 推送的 bridge_log 结构化日志后，
批量回采到 conversation-service 落库的接口：
  - POST /api/bridge-logs: 批量回采 terminal_bridge 执行日志（前端 -> conversation-service）

设计依据：
  - docs/solution/events/2026-07-20-terminal-bridge可观测性与日志回采重设计.md
  - docs/solution/events/2026-07-20-terminal-bridge回采链路断裂根因分析.md
  - OBS-TERMINAL-BRIDGE-001

鉴权（对齐现有 customer 路由 MVP 策略）：
  - 必须携带 Authorization: Bearer <session_token>
  - 接受 INTERNAL_API_TOKEN（内部服务调用）或占位符 token（customer 前端经网关兜底注入），
    对齐 agent_exec.py 的 _check_user_session 与 conversations.py 的 exec-result 路由强度；
  - 兼容既有 3 段 JWT 路径（解析 sub/user_id/user 用于审计）；
  - 缺失 / 非法一律 401；user_id 写入日志用于审计。
"""

from __future__ import annotations

import base64
import json
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field
from shared.database.postgres import DatabaseManager
from shared.observability.logger import get_logger
from sqlalchemy import text

from ..config import settings

logger = get_logger("bridge-logs-routes")
router = APIRouter(tags=["bridge-logs"])

_db_manager: DatabaseManager | None = None


def set_dependencies(db: DatabaseManager) -> None:
    """注入数据库依赖（由 main.py 在 lifespan 中调用）"""
    global _db_manager
    _db_manager = db


def _decode_jwt_payload(token: str) -> dict:
    """解码 JWT payload（不做签名校验），失败抛 401。"""
    parts = token.split(".")
    if len(parts) != 3:
        raise HTTPException(status_code=401, detail="Token 格式无效")
    try:
        payload_b64 = parts[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        return json.loads(base64.urlsafe_b64decode(payload_b64))
    except Exception:
        raise HTTPException(status_code=401, detail="Token 解析失败")


# customer 前端经网关兜底注入的占位符 token（对齐 conversations.py exec-result 路由）
_PLACEHOLDER_TOKEN = "client-session-placeholder-token"


def _parse_event_time(value: str | None) -> datetime | None:
    """把 Bridge RFC3339/RFC3339Nano 时间转换为 asyncpg 可绑定的 datetime。

    Go 默认输出纳秒精度的 RFC3339 时间，而 PostgreSQL/asyncpg 的 timestamptz
    参数要求 Python datetime。datetime.fromisoformat 会按数据库支持的微秒精度
    安全归一化多余的小数位，避免字符串在 prepared statement 绑定阶段被拒绝。

    Raises:
        ValueError: 时间格式非法或缺少时区。
    """
    if value is None:
        return None

    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        raise ValueError("event_time 必须包含时区")
    return parsed


def _check_session_or_internal(authorization: str | None = Header(default=None)) -> str:
    """回采接口鉴权（对齐现有 customer 路由 MVP 策略）。

    接受：
      - INTERNAL_API_TOKEN：内部服务调用（agent-service 等直接调用）
      - 占位符 token：customer 前端经 api-gateway 兜底注入（无真实 session 体系时的 MVP 路径）
      - 3 段 JWT：兼容既有携带真实 session 的调用，解析 sub/user_id/user 用于审计

    返回用户标识（user_id），供日志审计。任何非法情况均返回 401。
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="缺少 Bearer Token")
    token = authorization[7:].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Token 无效")

    # 1) 内部服务 token（服务间直接调用）
    if token == settings.INTERNAL_API_TOKEN:
        return "internal"

    # 2) 占位符 token（customer 前端经网关兜底，对齐 exec-result 路由）
    if token == _PLACEHOLDER_TOKEN:
        return "customer"

    # 3) 兼容既有 JWT 路径：3 段 JWT 解析 sub/user_id/user 用于审计
    if token.count(".") == 2:
        try:
            payload = _decode_jwt_payload(token)
            uid = payload.get("sub") or payload.get("user_id") or payload.get("user")
            if uid:
                return str(uid)
        except HTTPException:
            pass

    raise HTTPException(status_code=401, detail="Token 无效")


class BridgeLogEntry(BaseModel):
    """单条 bridge_log 结构"""

    case_id: str | None = None
    trace_id: str | None = None
    custom_ui: str | None = None
    user_id: str | None = None
    node_ip: str | None = None
    level: str = Field(default="INFO", pattern=r"^(DEBUG|INFO|WARN|ERROR)$")
    event: str
    message: str
    extra: dict[str, Any] | None = None
    event_id: str | None = None
    bridge_instance_id: str | None = None
    seq: int | None = Field(default=None, ge=0)
    ts: str | None = None
    span_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{16}$")
    trace_flags: str | None = Field(default=None, pattern=r"^[0-9a-f]{2}$")
    conversation_id: str | None = None
    exec_id: str | None = None
    tool_call_id: str | None = None
    service_name: str | None = Field(default=None, alias="service.name")
    service_version: str | None = Field(default=None, alias="service.version")
    deployment_environment: str | None = Field(default=None, alias="deployment.environment")

    model_config = {"populate_by_name": True}


class BridgeLogBatch(BaseModel):
    """批量 bridge_log"""

    logs: list[BridgeLogEntry]


@router.post("/api/bridge-logs")
async def ingest_bridge_logs(
    body: BridgeLogBatch,
    authorization: str | None = Header(default=None),
):
    """批量回采 terminal_bridge 结构化执行日志（前端 → conversation-service）。

    所有条目必须携带 case_id（无 case_id 的日志在浏览器端已被过滤，此处再次校验），
    落库到 bridge_execution_logs，供端到端可观测性与工单复盘。

    Args:
        body: 批量日志（logs）
        authorization: 用户 Session Token（真实鉴权）

    Returns:
        ok / 接收条数 / 跳过条数
    """
    user_id = _check_session_or_internal(authorization)

    if _db_manager is None:
        raise HTTPException(status_code=503, detail="数据库未就绪")

    accepted = 0
    duplicates = 0
    skipped = 0
    async for session in _db_manager.get_session():
        for entry in body.logs:
            if not entry.case_id:
                skipped += 1
                continue
            try:
                event_time = _parse_event_time(entry.ts)
            except (TypeError, ValueError):
                skipped += 1
                logger.warning(
                    event="bridge_log_invalid_event_time",
                    event_id=entry.event_id,
                    bridge_instance_id=entry.bridge_instance_id,
                    seq=entry.seq,
                )
                continue
            extra_json = json.dumps(entry.extra) if entry.extra else None
            extra = entry.extra or {}
            result = await session.execute(
                text(
                    """
                    INSERT INTO bridge_execution_logs
                        (case_id, trace_id, custom_ui, user_id, node_ip, level, event, message, extra,
                         event_id, bridge_instance_id, seq, event_time, span_id, trace_flags,
                         conversation_id, exec_id, tool_call_id, service_name, service_version,
                         deployment_environment, command, command_sha256, exit_code, duration_ms,
                         stdout_len, stderr_len, output_preview, success, error_type, stdout_sha256,
                         stderr_sha256, stdout_truncated, stderr_truncated, artifact_id)
                    VALUES
                        (:case_id, :trace_id, :custom_ui, :user_id, :node_ip, :level, :event, :message,
                         CAST(:extra AS jsonb), CAST(:event_id AS uuid), :bridge_instance_id, :seq,
                         CAST(:event_time AS timestamptz), :span_id, :trace_flags,
                         CAST(:conversation_id AS uuid), :exec_id, :tool_call_id, :service_name,
                         :service_version, :deployment_environment, :command, :command_sha256,
                         :exit_code, :duration_ms, :stdout_len, :stderr_len, :output_preview,
                         :success, :error_type, :stdout_sha256, :stderr_sha256,
                         :stdout_truncated, :stderr_truncated, CAST(:artifact_id AS uuid))
                    ON CONFLICT DO NOTHING
                    """
                ),
                {
                    "case_id": entry.case_id,
                    "trace_id": entry.trace_id,
                    "custom_ui": entry.custom_ui,
                    "user_id": entry.user_id or user_id,
                    "node_ip": entry.node_ip,
                    "level": (entry.level or "INFO").upper(),
                    "event": entry.event,
                    "message": entry.message,
                    "extra": extra_json,
                    "event_id": entry.event_id,
                    "bridge_instance_id": entry.bridge_instance_id,
                    "seq": entry.seq,
                    "event_time": event_time,
                    "span_id": entry.span_id,
                    "trace_flags": entry.trace_flags,
                    "conversation_id": entry.conversation_id,
                    "exec_id": entry.exec_id or extra.get("exec_id"),
                    "tool_call_id": entry.tool_call_id,
                    "service_name": entry.service_name or "terminal_bridge",
                    "service_version": entry.service_version,
                    "deployment_environment": entry.deployment_environment,
                    "command": extra.get("command_redacted"),
                    "command_sha256": extra.get("command_sha256"),
                    "exit_code": extra.get("exit_code"),
                    "duration_ms": extra.get("duration_ms"),
                    "stdout_len": extra.get("stdout_len") or extra.get("stdout_bytes"),
                    "stderr_len": extra.get("stderr_len") or extra.get("stderr_bytes"),
                    "output_preview": None,
                    "success": extra.get("success"),
                    "error_type": extra.get("error_type"),
                    "stdout_sha256": extra.get("stdout_sha256"),
                    "stderr_sha256": extra.get("stderr_sha256"),
                    "stdout_truncated": extra.get("stdout_truncated"),
                    "stderr_truncated": extra.get("stderr_truncated"),
                    "artifact_id": extra.get("artifact_id"),
                },
            )
            if result.rowcount == 0:
                duplicates += 1
            else:
                accepted += 1
        await session.commit()

    logger.info(
        event="bridge_logs_ingested",
        user_id=user_id,
        accepted=accepted,
        skipped=skipped,
        duplicates=duplicates,
    )
    return {"ok": True, "accepted": accepted, "duplicates": duplicates, "skipped": skipped}
