"""
KB Service — 知识命中统计路由（T3 / T4）

REST API：
- POST /api/kb/sop/{document_id}/hit           — SOP 文档命中 +1
- POST /api/kb/kbd/{kbd_id}/hit                — KBD 条目命中 +1
- POST /api/kb/kbd/{kbd_id}/hit/decrement      — KBD 条目命中 -1（admin 修正旧值时用）

鉴权：
- 使用 INTERNAL_API_TOKEN（内部服务调用）

设计说明：
- 所有计数使用 UPDATE ... SET hit_count = hit_count + 1 RETURNING 原子操作
- 禁止 SELECT + UPDATE 模式（存在竞态）
- hit_count 最小值为 0，decrement 使用 GREATEST(0, hit_count - 1)
- 所有操作记录结构化日志（包含 trace_id）
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request, status
from shared.utils.logger import get_logger
from sqlalchemy import update

from app.models.kbd_entry import KbdEntry
from app.models.sop_document import SopDocument

if TYPE_CHECKING:
    from shared.database.postgres import DatabaseManager

logger = get_logger("kb-service-hits")

sop_hit_router = APIRouter(prefix="/api/kb/sop", tags=["hits"])
kbd_hit_router = APIRouter(prefix="/api/kb/kbd", tags=["hits"])

# 由 main.py 注入
_db_manager: DatabaseManager | None = None


def set_dependencies(db: DatabaseManager) -> None:
    """注入 DB 依赖"""
    global _db_manager
    _db_manager = db


def _check_auth(request: Request) -> None:
    """验证内部服务 Token"""
    from app.config import settings

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="缺少 Bearer Token",
        )
    token = auth_header.split(" ", 1)[1]
    if token != settings.INTERNAL_API_TOKEN:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token 无效",
        )


# -------- SOP 命中接口 --------


@sop_hit_router.post("/{document_id}/hit")
async def increment_sop_hit(
    request: Request,
    document_id: int,
):
    """SOP 文档命中计数 +1（case 级去重由调用方保证）

    Args:
        document_id: sop_document.id

    Returns:
        { success: bool, document_id: int, hit_count: int }
    """
    _check_auth(request)

    if _db_manager is None:
        raise HTTPException(status_code=503, detail="服务未就绪")

    from shared.utils.trace import get_current_trace_id
    trace_id = get_current_trace_id()

    async with _db_manager.async_session_factory() as session:
        result = await session.execute(
            update(SopDocument)
            .where(SopDocument.id == document_id)
            .values(hit_count=SopDocument.hit_count + 1)
            .returning(SopDocument.id, SopDocument.hit_count)
        )
        row = result.one_or_none()
        if row is None:
            logger.warning(
                event="sop_hit_not_found",
                document_id=document_id,
                trace_id=trace_id,
            )
            raise HTTPException(status_code=404, detail=f"SOP 文档 {document_id} 不存在")

        await session.commit()
        logger.info(
            event="sop_hit_count_incremented",
            document_id=row[0],
            new_hit_count=row[1],
            trace_id=trace_id,
        )
        return {
            "success": True,
            "document_id": row[0],
            "hit_count": row[1],
        }


# -------- KBD 命中接口 --------


@kbd_hit_router.post("/{kbd_id}/hit")
async def increment_kbd_hit(
    request: Request,
    kbd_id: int,
):
    """KBD 条目命中计数 +1（S4 根因确认后触发，由调用方保证幂等）

    Args:
        kbd_id: kbd_entry.id

    Returns:
        { success: bool, kbd_id: int, hit_count: int }
    """
    _check_auth(request)

    if _db_manager is None:
        raise HTTPException(status_code=503, detail="服务未就绪")

    from shared.utils.trace import get_current_trace_id
    trace_id = get_current_trace_id()

    async with _db_manager.async_session_factory() as session:
        result = await session.execute(
            update(KbdEntry)
            .where(KbdEntry.id == kbd_id)
            .values(hit_count=KbdEntry.hit_count + 1)
            .returning(KbdEntry.id, KbdEntry.hit_count)
        )
        row = result.one_or_none()
        if row is None:
            logger.warning(
                event="kbd_hit_not_found",
                kbd_id=kbd_id,
                trace_id=trace_id,
            )
            raise HTTPException(status_code=404, detail=f"KBD 条目 {kbd_id} 不存在")

        await session.commit()
        logger.info(
            event="kbd_hit_count_incremented",
            kbd_id=row[0],
            new_hit_count=row[1],
            trace_id=trace_id,
        )
        return {
            "success": True,
            "kbd_id": row[0],
            "hit_count": row[1],
        }


@kbd_hit_router.post("/{kbd_id}/hit/decrement")
async def decrement_kbd_hit(
    request: Request,
    kbd_id: int,
):
    """KBD 条目命中计数 -1（admin 修正 resolved_kbd_entry_id 时扣减旧值）

    使用 GREATEST(0, hit_count - 1) 避免计数降为负数。

    Args:
        kbd_id: kbd_entry.id

    Returns:
        { success: bool, kbd_id: int, hit_count: int }
    """
    _check_auth(request)

    if _db_manager is None:
        raise HTTPException(status_code=503, detail="服务未就绪")

    from sqlalchemy import func
    from shared.utils.trace import get_current_trace_id
    trace_id = get_current_trace_id()

    async with _db_manager.async_session_factory() as session:
        result = await session.execute(
            update(KbdEntry)
            .where(KbdEntry.id == kbd_id)
            .values(hit_count=func.greatest(0, KbdEntry.hit_count - 1))
            .returning(KbdEntry.id, KbdEntry.hit_count)
        )
        row = result.one_or_none()
        if row is None:
            logger.warning(
                event="kbd_hit_decrement_not_found",
                kbd_id=kbd_id,
                trace_id=trace_id,
            )
            raise HTTPException(status_code=404, detail=f"KBD 条目 {kbd_id} 不存在")

        await session.commit()
        logger.info(
            event="kbd_hit_count_decremented",
            kbd_id=row[0],
            new_hit_count=row[1],
            trace_id=trace_id,
        )
        return {
            "success": True,
            "kbd_id": row[0],
            "hit_count": row[1],
        }
