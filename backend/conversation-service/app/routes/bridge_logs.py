"""
terminal_bridge 日志回采接口

设计依据：
  - docs/solution/events/... terminal_bridge 可观测性重设计（OBS-TERMINAL-BRIDGE-001）
  - Bridge 为通用代理，不感知后台地址；日志经浏览器（Custom-UI）以 `bridge_log`
    WebSocket 消息实时接收后，由前端统一 POST 到本接口落库，按 case_id / trace_id 关联。

鉴权：
  - /api/* 接口使用用户 Session Token（前端调用），与 submit_exec_result 保持一致。
  - P1-6: 支持 HMAC 签名验证（可选），防止前端日志内容篡改。
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text

from shared.database.postgres import DatabaseManager
from shared.observability.logger import get_logger
from shared.observability.otel import get_current_trace_id

from ..config import settings

logger = get_logger("bridge-log-routes")
router = APIRouter(tags=["bridge-logs"])

_db_manager: DatabaseManager | None = None


def set_dependencies(db: DatabaseManager) -> None:
    """注入数据库依赖"""
    global _db_manager
    _db_manager = db


def _check_user_session(authorization: str | None = Header(default=None)) -> str:
    """验证用户 Session Token（与 agent_exec.submit_exec_result 同款简化鉴权）。"""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="缺少 Bearer Token")
    token = authorization[7:].strip()
    if len(token) < 10:
        raise HTTPException(status_code=401, detail="Token 无效")
    return "user-placeholder"


class BridgeLogEntry(BaseModel):
    """单条 terminal_bridge 结构化日志"""

    seq: int | None = None
    ts: str | None = None
    level: str = Field("INFO", description="INFO/WARNING/ERROR")
    event: str | None = None
    message: str | None = None
    trace_id: str | None = None
    case_id: str | None = None
    node_ip: str | None = None
    custom_ui: str | None = None
    extra: dict[str, Any] | None = None
    signature: str | None = None  # P1-6: HMAC 签名（可选）


class BridgeLogBatchRequest(BaseModel):
    """批量回采请求（前端一次上报一批 bridge_log）"""

    logs: list[BridgeLogEntry] = Field(..., description="日志条目列表")


class BridgeLogResponse(BaseModel):
    ok: bool
    received: int
    persisted: int
    message: str


def _verify_log_signature(entry: BridgeLogEntry) -> bool:
    """P1-6: 验证日志签名，防止前端篡改（可选功能）"""
    if not entry.signature:
        # 未提供签名时跳过验证（MVP 阶段暂不强制）
        return True

    # 如果配置了 HMAC_KEY，则验证签名
    hmac_key = getattr(settings, "BRIDGE_LOG_HMAC_KEY", None)
    if not hmac_key:
        logger.warning(event="bridge_log_hmac_key_not_configured")
        return True

    # 构造待签名内容（不包含 signature 字段）
    entry_dict = entry.dict(exclude={"signature"})
    content = json.dumps(entry_dict, sort_keys=True, ensure_ascii=False)

    # 计算 HMAC-SHA256 签名
    expected_signature = hmac.new(
        hmac_key.encode(),
        content.encode(),
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(entry.signature, expected_signature)


@router.post(
    "/api/bridge-logs",
    response_model=BridgeLogResponse,
)
async def collect_bridge_logs(
    body: BridgeLogBatchRequest,
    user_id: str = Depends(_check_user_session),
):
    """接收 Custom-UI 回采的 terminal_bridge 日志并落库（按工单关联）。

    流程：
      1. 校验用户 Session Token
      2. P1-6: 验证 HMAC 签名（可选）
      3. 逐条 INSERT 到 bridge_execution_logs（case_id 关联；trace_id 关联端到端链路）
      4. 返回接收/落库计数

    Args:
        body: 批量日志（logs）
        user_id: 用户 ID

    Returns:
        回采结果（received / persisted 计数）
    """
    if _db_manager is None:
        raise HTTPException(status_code=503, detail="数据库服务未就绪")

    trace_id = get_current_trace_id()
    received = len(body.logs)
    persisted = 0
    signature_errors = 0

    async with _db_manager.get_session() as session:
        for entry in body.logs:
            if not entry.case_id:
                # 无工单关联视为无效，跳过（避免污染分析表）
                logger.warning(
                    event="bridge_log_skip_no_case",
                    level=entry.level,
                    event_name=entry.event,
                    trace_id=trace_id,
                )
                continue

            # P1-6: 验证签名（如果提供了签名）
            if entry.signature and not _verify_log_signature(entry):
                logger.warning(
                    event="bridge_log_signature_invalid",
                    level=entry.level,
                    event_name=entry.event,
                    case_id=entry.case_id,
                    trace_id=trace_id,
                )
                signature_errors += 1
                continue

            extra_json = json.dumps(entry.extra, ensure_ascii=False) if entry.extra else None
            await session.execute(
                text(
                    """
                    INSERT INTO bridge_execution_logs
                        (case_id, trace_id, custom_ui, node_ip, level, event, message, extra, user_id)
                    VALUES
                        (:case_id, :trace_id, :custom_ui, :node_ip, :level, :event, :message,
                         CAST(:extra AS jsonb), :user_id)
                    """
                ),
                {
                    "case_id": entry.case_id,
                    "trace_id": entry.trace_id,
                    "custom_ui": entry.custom_ui,
                    "node_ip": entry.node_ip,
                    "level": (entry.level or "INFO").upper(),
                    "event": entry.event,
                    "message": entry.message,
                    "extra": extra_json,
                    "user_id": user_id,  # P1-4: 记录操作用户 ID
                },
            )
            persisted += 1

    logger.info(
        event="bridge_logs_collected",
        received=received,
        persisted=persisted,
        signature_errors=signature_errors,
        user_id=user_id,
        trace_id=trace_id,
    )

    return BridgeLogResponse(
        ok=True,
        received=received,
        persisted=persisted,
        message=f"回采 {persisted}/{received} 条日志（签名验证失败 {signature_errors} 条）",
    )
