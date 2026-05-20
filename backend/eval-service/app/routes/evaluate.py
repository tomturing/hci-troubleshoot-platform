"""
Evaluation Routes - 用户评分API路由

提供用户主动评分接口和管理后台质量统计接口
"""

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from pydantic import BaseModel, Field
from shared.database.postgres import DatabaseManager
from shared.observability.logger import get_logger
from sqlalchemy import select

from sqlalchemy import text

from app.config import settings

from ..services.quality_score import QualityScoreService

router = APIRouter(prefix="/api", tags=["evaluation"])

# 全局依赖，需要在main.py中注入
database_manager: DatabaseManager | None = None

logger = get_logger("evaluation_routes")


def set_database_manager(db: DatabaseManager):
    """设置数据库管理器（在应用启动时调用）"""
    global database_manager
    database_manager = db


async def get_db_session():
    """获取数据库会话"""
    if not database_manager:
        raise HTTPException(status_code=500, detail="数据库服务未初始化")
    async for session in database_manager.get_session():
        yield session


def require_admin_token(authorization: str | None = Header(default=None)):
    """校验管理员 token（Bearer INTERNAL_API_TOKEN）"""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="缺少 Bearer Token")

    token = authorization[7:].strip()
    if token != settings.INTERNAL_API_TOKEN:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="无权限访问管理员接口")


# ============ Pydantic 模型 ============


class EvaluationCreate(BaseModel):
    """用户评分请求体"""

    score: int = Field(..., ge=1, le=5, description="用户评分（1-5星）")


class EvaluationResponse(BaseModel):
    """用户评分响应"""

    ok: bool = True
    evaluation_id: str | None = None
    score: int
    composite_score: int
    message: str = "感谢您的评价！"


class QualityStatsResponse(BaseModel):
    """质量统计响应"""

    period_days: int
    average_composite_score: float
    total_evaluated_cases: int
    low_score_cases: int
    low_score_rate: float
    close_reason_distribution: dict[str, int]
    user_rating_distribution: dict[str, int]


class LowScoreCaseItem(BaseModel):
    """低分工单项"""

    case_id: str
    conversation_id: str | None
    user_rating: int | None
    composite_score: int
    close_reason: str | None
    score_breakdown: Any | None
    evaluated_at: str | None
    case_title: str | None
    case_status: str | None


class LowScoreCasesResponse(BaseModel):
    """低分工件列表响应"""

    items: list[LowScoreCaseItem]
    total: int
    limit: int
    offset: int


# ============ 用户评分接口 ============


@router.post("/conversations/{conversation_id}/evaluate", response_model=EvaluationResponse)
async def submit_evaluation(
    conversation_id: uuid.UUID,
    evaluation: EvaluationCreate,
    session=Depends(get_db_session),
):
    """
    提交用户评分

    用户对对话进行 1-5 星评分，系统会：
    1. 验证评分范围（1-5）
    2. UPSERT assistant_evaluation 记录
    3. 重新计算 composite_score（加入用户满意度维度）
    4. 返回新的综合评分

    幂等设计：同一 conversation_id 重复调用返回已有评分，不报 409
    """
    # 1. 验证 conversation 存在，并获取 case_id
    conv_result = await session.execute(
        text("SELECT case_id FROM conversation WHERE conversation_id = :cid"),
        {"cid": str(conversation_id)},
    )
    conv_row = conv_result.fetchone()

    if not conv_row:
        logger.warning(
            event="evaluation_conversation_not_found",
            message=f"评分失败：对话不存在 {conversation_id}",
            conversation_id=str(conversation_id),
        )
        raise HTTPException(status_code=404, detail="对话不存在")

    case_id = str(conv_row[0])

    # 2. 初始化 QualityScoreService
    quality_service = QualityScoreService(session)

    # 3. 检查是否已评分（幂等性）
    from sqlalchemy import text

    existing_result = await session.execute(
        text(
            """
            SELECT score, composite_score
            FROM assistant_evaluation
            WHERE case_id = :case_id
            ORDER BY created_at DESC
            LIMIT 1
            """
        ),
        {"case_id": case_id},
    )
    existing = existing_result.fetchone()

    if existing and existing[0] is not None:
        # 已评分，直接返回已有记录（幂等）
        logger.info(
            event="evaluation_idempotent",
            message=f"重复评分请求：case_id={case_id}，返回已有评分",
            case_id=case_id,
            conversation_id=str(conversation_id),
            score=existing[0],
            composite_score=existing[1],
        )
        return EvaluationResponse(
            ok=True,
            score=existing[0],
            composite_score=existing[1] or 0,
            message="您已经评分过了，感谢您的反馈！",
        )

    # 4. 调用 QualityScoreService 更新评分
    try:
        composite_score = await quality_service.update_user_rating(
            case_id=case_id,
            conversation_id=str(conversation_id),
            user_rating=evaluation.score,
            trace_id=getattr(conversation, "trace_id", None),
        )

        logger.info(
            event="evaluation_submitted",
            message=f"用户评分提交成功：case_id={case_id}, score={evaluation.score}, composite_score={composite_score}",
            case_id=case_id,
            conversation_id=str(conversation_id),
            user_rating=evaluation.score,
            composite_score=composite_score,
        )

        return EvaluationResponse(
            ok=True,
            score=evaluation.score,
            composite_score=composite_score,
            message="感谢您的评价！",
        )

    except Exception as e:
        logger.error(
            event="evaluation_error",
            message=f"评分处理失败：{e}",
            case_id=case_id,
            conversation_id=str(conversation_id),
            error=str(e),
        )
        raise HTTPException(status_code=500, detail="评分处理失败，请稍后重试")


@router.get("/conversations/{conversation_id}/evaluation")
async def get_evaluation(
    conversation_id: uuid.UUID,
    session=Depends(get_db_session),
):
    """
    获取对话的评分信息

    返回该对话的评分详情，包括用户评分和综合评分
    """
    # 查询 conversation 获取 case_id
    # 获取 case_id（原始 SQL，eval-service 无 Conversation ORM）
    conv_row2 = (await session.execute(
        text("SELECT case_id FROM conversation WHERE conversation_id = :cid"),
        {"cid": str(conversation_id)},
    )).fetchone()

    if not conv_row2:
        raise HTTPException(status_code=404, detail="对话不存在")

    case_id = str(conv_row2[0])

    # 查询评分记录

    eval_result = await session.execute(
        text(
            """
            SELECT
                evaluation_id,
                score,
                composite_score,
                close_reason,
                score_breakdown,
                created_at,
                feedback
            FROM assistant_evaluation
            WHERE case_id = :case_id
            """
        ),
        {"case_id": case_id},
    )
    row = eval_result.fetchone()

    if not row:
        return {
            "has_evaluation": False,
            "message": "暂无评分记录",
        }

    return {
        "has_evaluation": True,
        "evaluation_id": str(row[0]),
        "user_rating": row[1],
        "composite_score": row[2],
        "close_reason": row[3],
        "score_breakdown": row[4],
        "evaluated_at": row[5].isoformat() if row[5] else None,
        "feedback": row[6],
    }


# ============ 管理后台接口 ============


@router.get("/admin/quality/stats", response_model=QualityStatsResponse)
async def get_quality_stats(
    days: int = Query(7, ge=1, le=90, description="统计天数（1-90天）"),
    _=Depends(require_admin_token),
    session=Depends(get_db_session),
):
    """
    [Admin] 获取质量统计

    返回近 N 天的质量统计数据：
    - 平均 composite_score
    - 各 close_reason 分布
    - 用户评分分布
    - 低分工件数量
    """
    # 使用 QualityStatsService 来获取统计
    from ..services.quality_score import QualityStatsService

    stats_service = QualityStatsService(session)
    stats = await stats_service.get_quality_stats(days=days)

    return QualityStatsResponse(
        period_days=stats["period_days"],
        average_composite_score=stats["average_composite_score"],
        total_evaluated_cases=stats["total_evaluated_cases"],
        low_score_cases=stats["low_score_cases"],
        low_score_rate=stats["low_score_rate"],
        close_reason_distribution=stats["close_reason_distribution"],
        user_rating_distribution=stats["user_rating_distribution"],
    )


@router.get("/admin/quality/cases", response_model=LowScoreCasesResponse)
async def get_low_score_cases(
    min_score: int = Query(0, ge=0, le=100, description="最低分数"),
    max_score: int = Query(39, ge=0, le=100, description="最高分数"),
    limit: int = Query(20, ge=1, le=100, description="每页数量"),
    offset: int = Query(0, ge=0, description="偏移量"),
    _=Depends(require_admin_token),
    session=Depends(get_db_session),
):
    """
    [Admin] 获取低分工件列表

    查询 composite_score 在指定范围内的工单列表，用于质量分析和改进。
    默认查询 composite_score < 40 的差 case。
    """
    from ..services.quality_score import QualityStatsService

    stats_service = QualityStatsService(session)
    result = await stats_service.get_low_score_cases(
        min_score=min_score,
        max_score=max_score,
        limit=limit,
        offset=offset,
    )

    # 将字典列表转换为 Pydantic 模型
    items = [LowScoreCaseItem(**item) for item in result["items"]]

    return LowScoreCasesResponse(
        items=items,
        total=result["total"],
        limit=result["limit"],
        offset=result["offset"],
    )
