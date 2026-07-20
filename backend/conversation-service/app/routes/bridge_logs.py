"""
terminal_bridge 日志回采接口 — 统一工单关联的日志落库

提供前端（Custom-UI）在收到 terminal_bridge 经 WebSocket 推送的 bridge_log 结构化日志后，
批量回采到 conversation-service 落库的接口：
  - POST /api/bridge-logs: 批量回采 terminal_bridge 执行日志（前端 → conversation-service）

设计依据：
  - docs/solution/events/2026-07-20-terminal-bridge可观测性与日志回采重设计.md
  - OBS-TERMINAL-BRIDGE-001

鉴权（真实 Session 鉴权，区别于 MVP 简化校验）：
  - 必须携带 Authorization: Bearer <session_token>
  - 优先调用 SESSION_VERIFY_URL（api-gateway 会话校验端点）做真实会话校验；
  - 否则在本地对 JWT 做结构校验 + 可选 HMAC(HS256) 签名校验（依赖 settings.SESSION_JWT_SECRET），
    从 payload 提取用户身份（sub / user_id / user）；
  - 缺失 / 非法一律 401，杜绝匿名回采；user_id 写入日志用于审计。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import urllib.parse
import urllib.request
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


def _verify_session(authorization: str | None = Header(default=None)) -> str:
    """真实用户 Session 鉴权（回采接口）。

    返回用户标识（user_id），供日志审计。任何非法情况均返回 401。
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="缺少 Bearer Token")
    token = authorization[7:].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Token 无效")

    # 1) 网关会话校验（真实 Session 校验的首选路径，需显式配置 SESSION_VERIFY_URL）
    verify_url = getattr(settings, "SESSION_VERIFY_URL", None)
    if verify_url:
        try:
            req = urllib.request.Request(
                f"{verify_url}?token={urllib.parse.quote(token)}",
                headers={"Authorization": f"Bearer {settings.INTERNAL_API_TOKEN}"},
            )
            with urllib.request.urlopen(req, timeout=3) as resp:  # noqa: S310
                if resp.status == 200:
                    data = json.loads(resp.read() or b"{}")
                    uid = data.get("user_id") or data.get("sub") or data.get("user")
                    return uid or "unknown"
            raise HTTPException(status_code=401, detail="会话校验失败")
        except HTTPException:
            raise
        except Exception:
            if getattr(settings, "SESSION_VERIFY_STRICT", False):
                raise HTTPException(status_code=503, detail="会话校验服务不可用")
            # 非严格模式：网关不可达时降级为本地 JWT 校验，避免阻断既有前端链路

    # 2) 本地 JWT 结构 + 可选 HMAC(HS256) 签名校验
    payload = _decode_jwt_payload(token)

    secret = getattr(settings, "SESSION_JWT_SECRET", None)
    if secret:
        parts = token.split(".")
        signing_input = f"{parts[0]}.{parts[1]}".encode()
        expected = hmac.new(secret.encode(), signing_input, hashlib.sha256).digest()
        expected_b64 = base64.urlsafe_b64encode(expected).rstrip(b"=").decode()
        if not hmac.compare_digest(expected_b64, parts[2]):
            raise HTTPException(status_code=401, detail="Token 签名无效")

    uid = payload.get("sub") or payload.get("user_id") or payload.get("user")
    if not uid:
        raise HTTPException(status_code=401, detail="Token 缺少用户身份")
    return uid


class BridgeLogEntry(BaseModel):
    """单条 bridge_log 结构化日志条目（与 terminal_bridge 的 logEntry 对齐）。"""

    seq: int | None = None
    ts: str | None = None
    level: str = Field("INFO", description="日志级别")
    event: str | None = None
    message: str | None = None
    case_id: str | None = Field(None, description="工单 ID（回采必须关联）")
    trace_id: str | None = None
    custom_ui: str | None = None
    node_ip: str | None = None
    user_id: str | None = None
    extra: dict[str, Any] | None = None


class BridgeLogBatch(BaseModel):
    """批量回采请求体。"""

    logs: list[BridgeLogEntry] = Field(..., description="结构化日志条目列表")


@router.post("/api/bridge-logs", status_code=202)
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
    user_id = _verify_session(authorization)

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
