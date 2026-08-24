"""qkv_vm_console 截图会话的管理端审计查询（设计文档 §7.3）。

- 仅返回脱敏摘要：不含目标验证明细、制品存储引用与原图字节；
- 支持工单、VM、状态、是否唤醒、视觉状态、KBD 修订与 Trace ID 过滤；
- 查看原图不在本端点提供（必须走授权端点并单独记审计）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from shared.database.postgres import DatabaseManager
from shared.observability.logger import get_logger
from sqlalchemy import text

logger = get_logger("vm-console-admin")

router = APIRouter(prefix="/api/admin/vm-console", tags=["vm-console-admin"])

_db_manager: DatabaseManager | None = None


def set_dependencies(db: DatabaseManager) -> None:
    """由 main.py 注入数据库依赖。"""

    global _db_manager
    _db_manager = db


def _check_auth(request: Request) -> None:
    from app.config import settings

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer ") or auth_header.split(" ", 1)[1] != settings.INTERNAL_API_TOKEN:
        raise HTTPException(status_code=401, detail="Token 无效")


@router.get("/captures")
async def list_captures(
    request: Request,
    case_id: str | None = Query(default=None, description="工单 ID 精确过滤"),
    vm_id: str | None = Query(default=None, description="VMID 精确过滤"),
    status_filter: str | None = Query(default=None, alias="status", description="状态机状态"),
    wake_state: str | None = Query(default=None, description="唤醒决定过滤"),
    vision_state: str | None = Query(default=None, description="视觉 display_state 过滤"),
    source_kbd_id: str | None = Query(default=None, description="KBD 来源过滤"),
    trace_id: str | None = Query(default=None, description="Trace ID 精确过滤"),
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """分页查询截图会话（脱敏摘要）。"""

    _check_auth(request)
    if _db_manager is None:
        raise HTTPException(status_code=503, detail="数据库未就绪")

    clauses: list[str] = []
    params: dict[str, Any] = {"limit": limit, "offset": offset}
    filters = {
        "case_id": case_id,
        "vm_id": vm_id,
        "status_filter": status_filter,
        "wake_state": wake_state,
        "source_kbd_id": source_kbd_id,
        "trace_id": trace_id,
    }
    columns = {
        "case_id": "case_id",
        "vm_id": "vm_id",
        "status_filter": "status",
        "wake_state": "wake_state",
        "source_kbd_id": "source_kbd_id",
        "trace_id": "trace_id",
    }
    for name, value in filters.items():
        if value:
            clauses.append(f"{columns[name]} = :{name}")
            params[name] = value
    if vision_state:
        clauses.append("vision_result ->> 'display_state' = :vision_state")
        params["vision_state"] = vision_state
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    async with _db_manager.async_session_factory() as session:
        total = (
            await session.execute(
                text(f"SELECT count(*) AS total FROM vm_console_capture {where}"), params
            )
        ).scalar_one()
        rows = (
            await session.execute(
                text(
                    f"""
                    SELECT capture_id::text, case_id, vm_id, host_node_id, mode, status,
                           error_code, signal_id, source_kbd_id, source_kbd_revision,
                           tool_catalog_revision, wake_state, wake_confirmed_by, wake_confirmed_at,
                           vision_result ->> 'display_state' AS vision_state,
                           vision_result ->> 'summary' AS vision_summary,
                           (vision_result ->> 'confidence')::float AS vision_confidence,
                           vision_model_revision,
                           (quality_metrics ->> 'near_black') AS near_black,
                           baseline_artifact_id IS NOT NULL AS has_baseline,
                           recapture_artifact_id IS NOT NULL AS has_recapture,
                           trace_id, created_at, completed_at
                    FROM vm_console_capture
                    {where}
                    ORDER BY created_at DESC
                    LIMIT :limit OFFSET :offset
                    """
                ),
                params,
            )
        ).mappings().all()

    return {
        "total": int(total),
        "limit": limit,
        "offset": offset,
        "items": [dict(row) for row in rows],
    }


@router.get("/captures/{capture_id}/events")
async def list_capture_events(
    request: Request,
    capture_id: str,
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, Any]:
    """查询单个截图会话的 append-only 审计事件流（§10.1）。"""

    _check_auth(request)
    if _db_manager is None:
        raise HTTPException(status_code=503, detail="数据库未就绪")

    async with _db_manager.async_session_factory() as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT event_id::text, event_type, actor, mode, detail, trace_id, created_at
                    FROM vm_console_audit_event
                    WHERE capture_id = CAST(:capture_id AS uuid)
                    ORDER BY created_at ASC
                    LIMIT :limit
                    """
                ),
                {"capture_id": capture_id, "limit": limit},
            )
        ).mappings().all()

    return {"capture_id": capture_id, "items": [dict(row) for row in rows]}


@router.get("/replay-fixtures")
async def replay_fixtures(request: Request) -> dict[str, Any]:
    """控制台截图回放 Fixture（§7.2）：五种确定性场景的回放结果。"""

    _check_auth(request)
    from shared.vision.replay_fixtures import run_replay

    return {"items": run_replay()}
