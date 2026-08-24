"""效果验证路由 — qkv_effect 结果卡推送与判定时间线查询。

提供效果验证的在线闭环接口：
  - POST /internal/conversations/{id}/effect-result: agent-service 推送三态判定
    结果卡（保存 message metadata kind=effect_result_card + SSE 推送）；
  - GET  /internal/conversations/{id}/effect-verifications: 查询会话的效果验证
    判定时间线（供 S6 证据化与 Admin 审计）。

设计依据：docs/solution/agent/效果验证生产者信号设计与需求.md §3.5/§5.3/§7.1。

鉴权：INTERNAL_API_TOKEN（内部服务调用，agent-service → conversation-service）。
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from shared.database.postgres import DatabaseManager
from shared.observability.logger import get_logger
from sqlalchemy import text

logger = get_logger("effect-verification-routes")
router = APIRouter(tags=["effect-verification"])

# 由 main.py 注入
_db_manager: DatabaseManager | None = None


def set_dependencies(db: DatabaseManager) -> None:
    """注入数据库依赖"""
    global _db_manager
    _db_manager = db


def _check_internal_auth(request: Request) -> None:
    from ..config import settings

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="缺少 Bearer Token")
    if auth_header.split(" ", 1)[1] != settings.INTERNAL_API_TOKEN:
        raise HTTPException(status_code=401, detail="Token 无效")


class EffectResultRequest(BaseModel):
    """效果验证结果卡载荷：三态判定 + 证据引用，无自由文本命令字段。"""

    model_config = {"extra": "forbid"}

    verification_id: str = Field(..., pattern=r"^[0-9a-fA-F-]{36}$")
    case_id: str = Field(..., min_length=1, max_length=32)
    signal_id: str | None = None
    verdict: str = Field(..., pattern=r"^(achieved|not_achieved|inconclusive)$")
    usage: str = Field(default="remediation_verify", pattern=r"^(remediation_verify|symptom_confirm)$")
    check_count: int = Field(default=1, ge=1)
    error_code: str | None = None
    checked_at: str | None = None
    trace_id: str | None = None


@router.post("/internal/conversations/{conversation_id}/effect-result", status_code=202)
async def push_effect_result(request: Request, conversation_id: uuid.UUID, body: EffectResultRequest):
    """保存效果验证结果卡（message metadata）并经 SSE 推送给活跃前端。"""

    _check_internal_auth(request)
    if _db_manager is None:
        raise HTTPException(status_code=503, detail="数据库未就绪")

    from app.models.message import Message, MessageRole

    metadata = {
        "kind": "effect_result_card",
        "verificationId": body.verification_id,
        "signalId": body.signal_id,
        "verdict": body.verdict,
        "usage": body.usage,
        "checkCount": body.check_count,
        "errorCode": body.error_code,
        "checkedAt": body.checked_at,
        "traceId": body.trace_id,
    }
    verdict_text = {
        "achieved": "复核完成：操作已达到预期效果。",
        "not_achieved": "复核完成：操作已执行，但未达到预期效果，建议重新诊断。",
        "inconclusive": "复核完成：观察不足以确认效果，请人工核实。",
    }[body.verdict]

    async for session in _db_manager.get_session():
        session.add(
            Message(
                conversation_id=conversation_id,
                case_id=body.case_id,
                role=MessageRole.ASSISTANT,
                content=verdict_text,
                metadata_=metadata,
            )
        )
        await session.commit()

    event_data = {
        "verificationId": body.verification_id,
        "signalId": body.signal_id,
        "verdict": body.verdict,
        "usage": body.usage,
        "checkCount": body.check_count,
        "errorCode": body.error_code,
        "checkedAt": body.checked_at,
        "traceId": body.trace_id,
    }
    sse_pusher = getattr(request.app.state, "sse_pusher", None)
    if sse_pusher:
        await sse_pusher.push_event(
            conversation_id=str(conversation_id), event_type="effect_result", data=event_data
        )
    else:
        logger.warning("effect_result_sse_pusher_unavailable", verification_id=body.verification_id)
    logger.info(
        "effect_result_pushed",
        conversation_id=str(conversation_id),
        verification_id=body.verification_id,
        verdict=body.verdict,
    )
    return {"ok": True, "verification_id": body.verification_id, "message": "效果验证结果已推送"}


@router.get("/internal/conversations/{conversation_id}/effect-verifications")
async def list_effect_verifications(request: Request, conversation_id: uuid.UUID) -> dict[str, Any]:
    """查询会话的效果验证判定时间线（S6 证据化 / Admin 审计）。"""

    _check_internal_auth(request)
    if _db_manager is None:
        raise HTTPException(status_code=503, detail="数据库未就绪")

    async for session in _db_manager.get_session():
        rows = (
            await session.execute(
                text(
                    """
                    SELECT v.verification_id::text, v.signal_id, v.usage, v.status, v.verdict,
                           v.verdict_vocabulary_revision, v.recheck_count, v.error_code,
                           v.created_at, v.completed_at
                    FROM effect_verification v
                    WHERE v.conversation_id = CAST(:conversation_id AS uuid)
                    ORDER BY v.created_at DESC
                    """
                ),
                {"conversation_id": str(conversation_id)},
            )
        ).mappings().all()
        checks = (
            await session.execute(
                text(
                    """
                    SELECT c.verification_id::text, c.check_seq, c.checked_at, c.trigger_source,
                           c.observation_status, c.check_verdict, c.error_code
                    FROM effect_verification_check c
                    JOIN effect_verification v ON v.verification_id = c.verification_id
                    WHERE v.conversation_id = CAST(:conversation_id AS uuid)
                    ORDER BY c.verification_id, c.check_seq
                    """
                ),
                {"conversation_id": str(conversation_id)},
            )
        ).mappings().all()

    verifications = []
    for row in rows:
        item = dict(row)
        item["checks"] = [dict(c) for c in checks if c["verification_id"] == row["verification_id"]]
        verifications.append(item)
    return {"conversation_id": str(conversation_id), "count": len(verifications), "verifications": verifications}


# 非终态集合（与 store EFFECT_STATUSES 同源口径）：用于进程重启后的孤儿回收。
_NON_TERMINAL_STATUSES = ("created", "expectation_resolved", "settle_pending", "observing", "recheck_scheduled")


async def reclaim_orphaned_verifications(db: DatabaseManager) -> int:
    """进程重启回收：把非终态效果验证标记为 inconclusive（fail-closed）。

    在线复核循环存活于 agent-service 请求进程内；会话中断后循环即消失，悬挂的
    复核不能伪装成“仍在进行”，也不能静默算作 achieved/not_achieved——按设计
    文档不变量 14 一律降级为 inconclusive + ORPHANED_BY_RESTART，交 S6 人工裁决。
    """

    from sqlalchemy import bindparam

    count = 0
    async for session in db.get_session():
        result = await session.execute(
            text(
                """
                UPDATE effect_verification
                SET status = 'verdict_inconclusive',
                    verdict = 'inconclusive',
                    error_code = 'ORPHANED_BY_RESTART',
                    error_summary = '诊断进程重启，复核循环中断；按观察不足处理，交人工裁决',
                    next_check_at = NULL,
                    completed_at = now(),
                    updated_at = now()
                WHERE status IN :statuses
                """
            ).bindparams(bindparam("statuses", expanding=True)),
            {"statuses": list(_NON_TERMINAL_STATUSES)},
        )
        await session.commit()
        count = int(result.rowcount or 0)
    if count:
        logger.info("effect_verification_orphans_reclaimed", count=count)
    return count
