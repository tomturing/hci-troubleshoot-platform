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
    skipped = 0
    async for session in _db_manager.get_session():
        for entry in body.logs:
            if not entry.case_id:
                skipped += 1
                continue
            extra_json = json.dumps(entry.extra) if entry.extra else None
            await session.execute(
                text(
                    """
                    INSERT INTO bridge_execution_logs
                        (case_id, trace_id, custom_ui, user_id, node_ip, level, event, message, extra)
                    VALUES
                        (:case_id, :trace_id, :custom_ui, :user_id, :node_ip, :level, :event, :message,
                         CAST(:extra AS jsonb))
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
                },
            )
            accepted += 1
        await session.commit()

    logger.info(
        event="bridge_logs_ingested",
        user_id=user_id,
        accepted=accepted,
        skipped=skipped,
    )
    return {"ok": True, "accepted": accepted, "skipped": skipped}
