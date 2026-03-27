"""
Admin Quality Routes - 质量管理 API

提供质量评分的管理查询接口：
- GET /api/admin/quality/stats  - 近 N 天汇总统计
- GET /api/admin/quality/cases  - 按分数范围筛选工单
"""

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from shared.database.postgres import DatabaseManager
from sqlalchemy import func, select

from ..models import AssistantEvaluation

router = APIRouter(prefix="/api/admin/quality", tags=["admin-quality"])

# 由 main.py lifespan 注入
database_manager: DatabaseManager | None = None


def set_database_manager(db_manager: DatabaseManager):
    global database_manager
    database_manager = db_manager


async def get_db_session():
    if not database_manager:
        raise HTTPException(status_code=500, detail="Database not initialized")
    async for session in database_manager.get_session():
        yield session


@router.get("/stats")
async def get_quality_stats(
    days: int = Query(7, ge=1, le=90, description="统计天数"),
    session=Depends(get_db_session),
):
    """[Admin] 近 N 天质量评分汇总统计"""
    since = datetime.now(UTC) - timedelta(days=days)

    # 总体平均分和评分工单数
    overall_stmt = select(
        func.avg(AssistantEvaluation.composite_score).label("avg_score"),
        func.count(AssistantEvaluation.evaluation_id).label("total"),
    ).where(
        AssistantEvaluation.created_at >= since,
        AssistantEvaluation.composite_score.isnot(None),
    )
    overall_result = await session.execute(overall_stmt)
    row = overall_result.one()

    # 按 close_reason 分组
    by_reason_stmt = (
        select(
            AssistantEvaluation.close_reason,
            func.avg(AssistantEvaluation.composite_score).label("avg_score"),
            func.count(AssistantEvaluation.evaluation_id).label("count"),
        )
        .where(
            AssistantEvaluation.created_at >= since,
            AssistantEvaluation.composite_score.isnot(None),
        )
        .group_by(AssistantEvaluation.close_reason)
    )
    by_reason_result = await session.execute(by_reason_stmt)

    breakdown = [
        {
            "close_reason": r.close_reason or "unknown",
            "avg_score": round(float(r.avg_score or 0), 1),
            "count": r.count,
        }
        for r in by_reason_result
    ]

    return {
        "period_days": days,
        "total_evaluated_cases": row.total or 0,
        "average_composite_score": round(float(row.avg_score or 0), 1),
        "breakdown_by_close_reason": breakdown,
    }


@router.get("/cases")
async def get_quality_cases(
    max_score: float = Query(40.0, ge=0, le=100, description="最高分筛选阈值（含）"),
    min_score: float = Query(0.0, ge=0, le=100, description="最低分筛选阈值（含）"),
    close_reason: str | None = Query(None, description="按关闭原因筛选"),
    limit: int = Query(20, ge=1, le=100, description="每页数量"),
    offset: int = Query(0, ge=0, description="偏移量"),
    session=Depends(get_db_session),
):
    """[Admin] 查询指定分数范围的工单（默认返回低质量工单 composite_score < 40）"""
    conditions = [
        AssistantEvaluation.composite_score >= min_score,
        AssistantEvaluation.composite_score <= max_score,
        AssistantEvaluation.composite_score.isnot(None),
    ]
    if close_reason:
        conditions.append(AssistantEvaluation.close_reason == close_reason)

    stmt = (
        select(
            AssistantEvaluation.case_id,
            AssistantEvaluation.composite_score,
            AssistantEvaluation.score_breakdown,
            AssistantEvaluation.close_reason,
            AssistantEvaluation.created_at,
        )
        .where(*conditions)
        .order_by(AssistantEvaluation.composite_score.asc())
        .offset(offset)
        .limit(limit)
    )
    result = await session.execute(stmt)

    cases = [
        {
            "case_id": r.case_id,
            "composite_score": r.composite_score,
            "score_breakdown": r.score_breakdown,
            "close_reason": r.close_reason,
            "evaluated_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in result
    ]

    return {
        "total_returned": len(cases),
        "offset": offset,
        "limit": limit,
        "filters": {
            "min_score": min_score,
            "max_score": max_score,
            "close_reason": close_reason,
        },
        "cases": cases,
    }
