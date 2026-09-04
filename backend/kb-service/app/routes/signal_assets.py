"""
backend/kb-service/app/routes/signal_assets.py
关键信号建模资产管理只读接口：
- signal_modeling_template: 13 类信号输入 Schema 与参数契约模板
- signal_best_practice: 专家审核黄金实例库 (Few-Shot 样本)
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from shared.database.postgres import DatabaseManager
from shared.observability.logger import get_logger
from sqlalchemy import text

logger = get_logger("signal-assets-admin")

router = APIRouter(prefix="/api/admin/signal-assets", tags=["signal-assets-admin"])

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


@router.get("/templates")
async def list_templates(
    request: Request,
    category: str | None = Query(default=None, description="分类过滤 (frontend/backend)"),
    tool_name: str | None = Query(default=None, description="工具名过滤"),
    active_only: bool = Query(default=True, description="是否仅查询激活模板"),
) -> dict[str, Any]:
    """获取所有 13 类关键信号建模契约标准模板。"""
    _check_auth(request)
    if _db_manager is None:
        raise HTTPException(status_code=503, detail="数据库未就绪")

    clauses: list[str] = []
    params: dict[str, Any] = {}

    if active_only:
        clauses.append("is_active = TRUE")
    if category:
        clauses.append("category = :category")
        params["category"] = category
    if tool_name:
        clauses.append("tool_name ILIKE :tool_name")
        params["tool_name"] = f"%{tool_name}%"

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    async with _db_manager.async_session_factory() as session:
        rows = (
            (
                await session.execute(
                    text(
                        f"""
                    SELECT id, tool_name, category, description,
                           acquire_schema, allowed_matcher_types,
                           variable_protocol, anti_patterns,
                           is_active, trace_id, created_at, updated_at
                    FROM signal_modeling_template
                    {where}
                    ORDER BY category ASC, tool_name ASC
                    """
                    ),
                    params,
                )
            )
            .mappings()
            .all()
        )

    items = [
        {
            "id": row["id"],
            "tool_name": row["tool_name"],
            "category": row["category"],
            "description": row["description"],
            "acquire_schema": row["acquire_schema"] or {},
            "allowed_matcher_types": row["allowed_matcher_types"] or [],
            "variable_protocol": row["variable_protocol"] or {},
            "anti_patterns": row["anti_patterns"] or [],
            "is_active": row["is_active"],
            "trace_id": row["trace_id"],
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
            "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
        }
        for row in rows
    ]

    return {"total": len(items), "items": items}


@router.get("/best-practices")
async def list_best_practices(
    request: Request,
    tool_name: str | None = Query(default=None, description="工具名精确过滤"),
    pattern_category: str | None = Query(default=None, description="模式分类过滤"),
    support_id: str | None = Query(default=None, description="案例 Support ID 过滤"),
    search: str | None = Query(default=None, description="关键字模糊搜索 (support_id/tool_name/notes)"),
    active_only: bool = Query(default=True, description="是否仅查询激活实践"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """分页查询信号最佳实践黄金实例库。"""
    _check_auth(request)
    if _db_manager is None:
        raise HTTPException(status_code=503, detail="数据库未就绪")

    clauses: list[str] = []
    params: dict[str, Any] = {"limit": limit, "offset": offset}

    if active_only:
        clauses.append("is_active = TRUE")
    if tool_name:
        clauses.append("tool_name = :tool_name")
        params["tool_name"] = tool_name
    if pattern_category:
        clauses.append("pattern_category = :pattern_category")
        params["pattern_category"] = pattern_category
    if support_id:
        clauses.append("support_id = :support_id")
        params["support_id"] = support_id
    if search:
        clauses.append(
            "(support_id ILIKE :search OR tool_name ILIKE :search OR pattern_category ILIKE :search OR design_notes ILIKE :search)"
        )
        params["search"] = f"%{search}%"

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    async with _db_manager.async_session_factory() as session:
        total = (
            await session.execute(text(f"SELECT count(*) AS total FROM signal_best_practice {where}"), params)
        ).scalar_one()

        rows = (
            (
                await session.execute(
                    text(
                        f"""
                    SELECT id, template_id, tool_name, pattern_category,
                           source_kbd_id, support_id, raw_evidence,
                           signal_json, design_notes, completeness_score,
                           is_active, trace_id, created_at, updated_at
                    FROM signal_best_practice
                    {where}
                    ORDER BY completeness_score DESC, id DESC
                    LIMIT :limit OFFSET :offset
                    """
                    ),
                    params,
                )
            )
            .mappings()
            .all()
        )

    items = [
        {
            "id": row["id"],
            "template_id": row["template_id"],
            "tool_name": row["tool_name"],
            "pattern_category": row["pattern_category"],
            "source_kbd_id": row["source_kbd_id"],
            "support_id": row["support_id"],
            "signal_title": (row["signal_json"] or {}).get("title") or (row["signal_json"] or {}).get("name") or "",
            "raw_evidence": row["raw_evidence"],
            "signal_json": row["signal_json"] or {},
            "design_notes": row["design_notes"] or "",
            "completeness_score": row["completeness_score"],
            "is_active": row["is_active"],
            "trace_id": row["trace_id"],
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
            "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
        }
        for row in rows
    ]

    return {
        "total": int(total),
        "limit": limit,
        "offset": offset,
        "items": items,
    }


@router.get("/best-practices/{practice_id}")
async def get_best_practice(
    request: Request,
    practice_id: int,
) -> dict[str, Any]:
    """获取单条最佳实践黄金实例完整详情。"""
    _check_auth(request)
    if _db_manager is None:
        raise HTTPException(status_code=503, detail="数据库未就绪")

    async with _db_manager.async_session_factory() as session:
        row = (
            (
                await session.execute(
                    text(
                        """
                    SELECT id, template_id, tool_name, pattern_category,
                           source_kbd_id, support_id, raw_evidence,
                           signal_json, design_notes, completeness_score,
                           is_active, trace_id, created_at, updated_at
                    FROM signal_best_practice
                    WHERE id = :practice_id
                    """
                    ),
                    {"practice_id": practice_id},
                )
            )
            .mappings()
            .first()
        )

    if not row:
        raise HTTPException(status_code=404, detail="最佳实践记录不存在")

    return {
        "id": row["id"],
        "template_id": row["template_id"],
        "tool_name": row["tool_name"],
        "pattern_category": row["pattern_category"],
        "source_kbd_id": row["source_kbd_id"],
        "support_id": row["support_id"],
        "signal_title": (row["signal_json"] or {}).get("title") or (row["signal_json"] or {}).get("name") or "",
        "raw_evidence": row["raw_evidence"],
        "signal_json": row["signal_json"] or {},
        "design_notes": row["design_notes"] or "",
        "completeness_score": row["completeness_score"],
        "is_active": row["is_active"],
        "trace_id": row["trace_id"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
    }


@router.get("/failures")
async def list_failures(
    request: Request,
    kbd_id: int | None = Query(default=None, description="KBD 内部自增 ID 过滤"),
    support_id: str | None = Query(default=None, description="Support 案例 ID 过滤"),
    stage: str | None = Query(default=None, description="抽取阶段 (count/classify/modeling/verification)"),
    reason: str | None = Query(default=None, description="失败原因分类过滤"),
    trace_id: str | None = Query(default=None, description="调用链 Trace ID 过滤"),
    resolved: bool | None = Query(default=None, description="是否已解决过滤"),
    limit: int = Query(default=20, ge=1, le=100, description="分页限制"),
    offset: int = Query(default=0, ge=0, description="分页偏移"),
) -> dict[str, Any]:
    """分页查询信号抽取异常复盘日志 (signal_failure_extraction)。"""
    _check_auth(request)
    if _db_manager is None:
        raise HTTPException(status_code=503, detail="数据库未就绪")

    clauses: list[str] = []
    params: dict[str, Any] = {"limit": limit, "offset": offset}

    if isinstance(kbd_id, int):
        clauses.append("f.kbd_id = :kbd_id")
        params["kbd_id"] = kbd_id
    if isinstance(support_id, str) and support_id.strip():
        clauses.append("((ke.support_id ILIKE :support_id) OR (f.detail_payload->>'support_id' ILIKE :support_id))")
        params["support_id"] = f"%{support_id.strip()}%"
    if isinstance(stage, str) and stage.strip():
        clauses.append("f.stage = :stage")
        params["stage"] = stage.strip()
    if isinstance(reason, str) and reason.strip():
        clauses.append("f.reason ILIKE :reason")
        params["reason"] = f"%{reason.strip()}%"
    if isinstance(trace_id, str) and trace_id.strip():
        clauses.append("f.trace_id ILIKE :trace_id")
        params["trace_id"] = f"%{trace_id.strip()}%"
    if isinstance(resolved, bool):
        clauses.append("f.resolved = :resolved")
        params["resolved"] = resolved

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    async with _db_manager.async_session_factory() as session:
        total_res = await session.execute(
            text(
                f"""
                SELECT COUNT(*)
                FROM signal_failure_extraction f
                LEFT JOIN kbd_entry ke ON f.kbd_id = ke.id
                {where}
                """
            ),
            params,
        )
        total = total_res.scalar_one()

        rows = (
            (
                await session.execute(
                    text(
                        f"""
                        SELECT f.id, f.kbd_id, f.stage, f.raw_content, f.reason,
                               f.detail_payload, f.trace_id, f.resolved,
                               f.resolved_by, f.resolved_notes, f.created_at, f.updated_at,
                               ke.support_id AS kbd_support_id, ke.title AS kbd_title
                        FROM signal_failure_extraction f
                        LEFT JOIN kbd_entry ke ON f.kbd_id = ke.id
                        {where}
                        ORDER BY f.id DESC
                        LIMIT :limit OFFSET :offset
                        """
                    ),
                    params,
                )
            )
            .mappings()
            .all()
        )

    items = [
        {
            "id": row["id"],
            "kbd_id": row["kbd_id"],
            "support_id": row["kbd_support_id"] or (row["detail_payload"] or {}).get("support_id") or "",
            "kbd_title": row["kbd_title"] or "",
            "stage": row["stage"],
            "reason": row["reason"],
            "raw_content_preview": (row["raw_content"][:300] + "...") if row["raw_content"] and len(row["raw_content"]) > 300 else (row["raw_content"] or ""),
            "raw_content": row["raw_content"],
            "detail_payload": row["detail_payload"] or {},
            "trace_id": row["trace_id"],
            "resolved": row["resolved"],
            "resolved_by": row["resolved_by"],
            "resolved_notes": row["resolved_notes"],
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
            "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
        }
        for row in rows
    ]

    return {
        "total": int(total),
        "limit": limit,
        "offset": offset,
        "items": items,
    }


@router.get("/failures/{failure_id}")
async def get_failure(
    request: Request,
    failure_id: int,
) -> dict[str, Any]:
    """获取单条信号抽取失败复盘日志完整详情。"""
    _check_auth(request)
    if _db_manager is None:
        raise HTTPException(status_code=503, detail="数据库未就绪")

    async with _db_manager.async_session_factory() as session:
        row = (
            (
                await session.execute(
                    text(
                        """
                        SELECT f.id, f.kbd_id, f.stage, f.raw_content, f.reason,
                               f.detail_payload, f.trace_id, f.resolved,
                               f.resolved_by, f.resolved_notes, f.created_at, f.updated_at,
                               ke.support_id AS kbd_support_id, ke.title AS kbd_title
                        FROM signal_failure_extraction f
                        LEFT JOIN kbd_entry ke ON f.kbd_id = ke.id
                        WHERE f.id = :failure_id
                        """
                    ),
                    {"failure_id": failure_id},
                )
            )
            .mappings()
            .first()
        )

    if not row:
        raise HTTPException(status_code=404, detail="信号抽取异常记录不存在")

    return {
        "id": row["id"],
        "kbd_id": row["kbd_id"],
        "support_id": row["kbd_support_id"] or (row["detail_payload"] or {}).get("support_id") or "",
        "kbd_title": row["kbd_title"] or "",
        "stage": row["stage"],
        "reason": row["reason"],
        "raw_content": row["raw_content"],
        "detail_payload": row["detail_payload"] or {},
        "trace_id": row["trace_id"],
        "resolved": row["resolved"],
        "resolved_by": row["resolved_by"],
        "resolved_notes": row["resolved_notes"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
        "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
    }

