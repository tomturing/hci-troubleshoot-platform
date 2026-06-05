import logging
from typing import Any

from fastapi import APIRouter, Depends, Query
from shared.database.postgres import DatabaseManager
from shared.models.audit import AuditLog, ToolAuditLog
from shared.models.conversation import Conversation
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/audit-logs", tags=["audit"])

# 由 main.py 注入数据库管理器
database_manager: DatabaseManager | None = None


def set_audit_database_manager(db: DatabaseManager) -> None:
    """由 main.py 在 lifespan 中调用，注入数据库管理器"""
    global database_manager
    database_manager = db


async def get_db() -> AsyncSession:
    """依赖项：获取数据库会话"""
    if not database_manager:
        from fastapi import HTTPException

        raise HTTPException(status_code=500, detail="数据库未初始化")
    async for session in database_manager.get_session():
        yield session


@router.get("", summary="查询工具调用审计日志（只读）")
async def list_audit_logs(
    session_id: str | None = Query(None, description="按会话 ID 过滤"),
    tool_name: str | None = Query(None, description="按工具名称过滤"),
    risk_level: int | None = Query(None, description="按风险等级过滤（1/2/3）"),
    limit: int = Query(50, ge=1, le=200, description="每页条数"),
    offset: int = Query(0, ge=0, description="分页偏移量"),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """
    查询工具调用审计日志。

    只读接口，不提供删除功能（审计记录不可删除）。
    """
    stmt = select(ToolAuditLog).order_by(ToolAuditLog.started_at.desc())

    # 动态过滤条件
    if session_id:
        stmt = stmt.where(ToolAuditLog.session_id == session_id)
    if tool_name:
        stmt = stmt.where(ToolAuditLog.tool_name == tool_name)
    if risk_level is not None:
        stmt = stmt.where(ToolAuditLog.risk_level == risk_level)

    # 使用分页查询
    paginated_stmt = stmt.limit(limit).offset(offset)
    result = await db.execute(paginated_stmt)
    logs = result.scalars().all()

    return {
        "total": len(logs),  # 当前页数量（精确分页需另行 COUNT 查询）
        "limit": limit,
        "offset": offset,
        "items": [
            {
                "id": log.id,
                "session_id": log.session_id,
                "tool_name": log.tool_name,
                "tool_args": log.tool_args,
                "risk_level": log.risk_level,
                "policy": log.policy,
                "authorized_by": log.authorized_by,
                "result": log.result,
                "error": log.error,
                "started_at": log.started_at.isoformat() if log.started_at else None,
                "completed_at": log.completed_at.isoformat() if log.completed_at else None,
                "duration_ms": log.duration_ms,
                "trace_id": log.trace_id,
            }
            for log in logs
        ],
    }


@router.get("/prompts", summary="查询Prompt审计日志（只读）")
async def list_prompt_audit_logs(
    case_id: str | None = Query(None, description="按工单 ID 过滤"),
    conversation_id: str | None = Query(None, description="按会话 ID 过滤"),
    trace_id: str | None = Query(None, description="按 Trace ID 过滤"),
    limit: int = Query(50, ge=1, le=200, description="每页条数"),
    offset: int = Query(0, ge=0, description="分页偏移量"),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """
    查询 Prompt 审计日志，支持按 case_id, conversation_id, trace_id 过滤。
    """
    stmt = select(AuditLog, Conversation.case_id).join(
        Conversation, AuditLog.conversation_id == Conversation.conversation_id
    )

    # 动态过滤条件
    if case_id:
        stmt = stmt.where(Conversation.case_id == case_id)
    if conversation_id:
        stmt = stmt.where(AuditLog.conversation_id == conversation_id)
    if trace_id:
        stmt = stmt.where(AuditLog.trace_id == trace_id)

    # 精确总数统计
    count_stmt = select(func.count()).select_from(stmt.subquery())
    count_result = await db.execute(count_stmt)
    total = count_result.scalar_one()

    # 添加排序和分页
    stmt = stmt.order_by(AuditLog.started_at.desc())
    paginated_stmt = stmt.limit(limit).offset(offset)
    result = await db.execute(paginated_stmt)
    rows = result.all()

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": [
            {
                "id": log.id,
                "audit_type": log.audit_type,
                "conversation_id": str(log.conversation_id),
                "case_id": case_id_val,
                "turn_index": log.turn_index,
                "system_prompt_id": log.system_prompt_id,
                "payload": log.payload,
                "error": log.error,
                "duration_ms": log.duration_ms,
                "started_at": log.started_at.isoformat() if log.started_at else None,
                "completed_at": log.completed_at.isoformat() if log.completed_at else None,
                "trace_id": log.trace_id,
            }
            for log, case_id_val in rows
        ],
    }
