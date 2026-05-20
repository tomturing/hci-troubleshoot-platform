"""
Quality Score Service - 综合质量评分服务

实现双轨制质量评价体系：被动隐式信号（100% 覆盖）+ 主动显式评分
"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from shared.observability.logger import get_logger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = get_logger("quality_score")

# 关闭原因评分映射（与 case-service/quality_score.py 保持严格一致，避免重算时分值漂移）
CLOSE_REASON_SCORE = {
    "user_command": 100,  # 用户主动解决：最高分
    "timeout": 50,  # 超时：中性基础分
    "admin_close": 50,  # 人工干预：略高于中性
    "abandon": 10,  # 放弃：最低分
    None: 50,  # 未知：中性
}

# 基础权重配置（归一化前）
BASE_WEIGHTS = {
    "user_rating": 0.30,  # 用户满意度（无评分时降为 0）
    "close_intent": 0.20,  # 关闭意图
    "efficiency": 0.20,  # 解决效率
    "repeat_penalty": 0.15,  # 用户重复提问（负向信号）
    "ai_quality": 0.15,  # AI 能力质量
}


@dataclass
class QualitySignals:
    """质量信号数据类"""

    case_id: str
    close_reason: str | None  # "user_command" | "timeout" | "abandon" | "admin_close" | None
    session_duration_sec: int | None
    message_count: int | None
    repeat_question_count: int  # 相似重复提问次数，始终有值
    user_rating: int | None  # 1-5，用户未评分则 None
    has_sop: bool | None  # 来自 prompt_audit 元数据
    kb_chunks_count: int | None  # 来自 prompt_audit 元数据
    kb_top_score: float | None  # 来自 prompt_audit 元数据


@dataclass
class QualityScore:
    """质量评分结果数据类"""

    composite_score: int  # 0-100 最终综合分
    rating_included: bool  # 是否含用户评分维度
    breakdown: dict[str, int]  # 各维度详细分解


def _timeout_score(session_duration_sec: int) -> int:
    """超时情况下的评分修正"""
    if session_duration_sec > 3600:  # > 1 小时才超时 → 很可能已解决
        return 70
    if session_duration_sec > 1800:  # 30-60 分钟
        return 55
    return 40  # < 30 分钟就超时 → 可能放弃


def _duration_score(session_duration_sec: int | None) -> int:
    """会话时长评分（0-100）"""
    if session_duration_sec is None:
        return 50
    if session_duration_sec < 300:  # < 5 min
        return 100
    if session_duration_sec < 900:  # 5-15 min
        return 80
    if session_duration_sec < 1800:  # 15-30 min
        return 60
    if session_duration_sec < 3600:  # 30-60 min
        return 40
    return 20  # > 60 min


def _message_count_score(message_count: int | None) -> int:
    """消息轮数评分（0-100）"""
    if message_count is None:
        return 50
    if message_count <= 3:
        return 100
    if message_count <= 8:
        return 80
    if message_count <= 15:
        return 60
    if message_count <= 25:
        return 40
    return 20


def _repeat_question_score(repeat_count: int) -> int:
    """重复提问评分（0-100）"""
    if repeat_count == 0:
        return 100  # 无重复，AI 一次回答即解决
    if repeat_count == 1:
        return 70  # 轻微重复，可接受
    if repeat_count == 2:
        return 45  # 明显重复，AI 回答有问题
    if repeat_count == 3:
        return 20  # 严重重复，AI 基本无效
    return 0  # ≥4 次重复，完全失败


def _ai_quality_score(has_sop: bool, kb_chunks_count: int, kb_top_score: float) -> int:
    """AI 能力质量评分（0-100）"""
    score = 0
    # SOP 命中 +40 基础分
    if has_sop:
        score += 40
    # KB 命中质量（相关度加权）
    if kb_chunks_count > 0:
        score += min(60, int(kb_top_score * 60))  # 最高 60 分
    return min(100, score)


def compute_quality_score(signals: QualitySignals) -> QualityScore:
    """
    计算综合质量评分

    Args:
        signals: 质量信号数据

    Returns:
        QualityScore: 包含综合分、是否含用户评分、各维度分解
    """
    raw_weights = dict(BASE_WEIGHTS)
    dim_scores: dict[str, int] = {}

    # 维度 2：关闭意图（始终参与）
    close_score = CLOSE_REASON_SCORE.get(signals.close_reason, 50)
    if signals.close_reason == "timeout" and signals.session_duration_sec:
        close_score = _timeout_score(signals.session_duration_sec)
    dim_scores["close_intent"] = close_score

    # 维度 3：解决效率（始终参与）
    duration_s = _duration_score(signals.session_duration_sec)
    msg_s = _message_count_score(signals.message_count)
    dim_scores["efficiency"] = (duration_s + msg_s) // 2

    # 维度 4：用户重复提问（始终参与，100% 覆盖）
    dim_scores["repeat_penalty"] = _repeat_question_score(signals.repeat_question_count)

    # 维度 5：AI 能力质量（prompt_audit 元数据，100% 覆盖；极少情况下为 None 则跳过）
    if signals.has_sop is not None and signals.kb_chunks_count is not None:
        dim_scores["ai_quality"] = _ai_quality_score(
            signals.has_sop, signals.kb_chunks_count, signals.kb_top_score or 0.0
        )
    else:
        raw_weights.pop("ai_quality")  # 无数据时权重转移到其他维度

    # 维度 1：用户满意度（仅有评分时参与）
    if signals.user_rating is not None:
        # 星级 1-5 映射到 0-100
        dim_scores["user_rating"] = (signals.user_rating - 1) * 25
    else:
        raw_weights.pop("user_rating")  # 无评分时权重归零，其余维度等比放大

    # 归一化加权平均（确保权重之和 = 1.0）
    active_dims = list(dim_scores.keys())
    total_weight = sum(raw_weights[k] for k in active_dims)
    composite = sum(dim_scores[k] * raw_weights[k] for k in active_dims) / total_weight

    return QualityScore(
        composite_score=round(composite),
        rating_included=signals.user_rating is not None,
        breakdown={k: round(dim_scores[k]) for k in active_dims},
    )


class QualityScoreService:
    """综合质量评分服务

    职责：
      1. 采集所有可用的质量信号
      2. 计算 composite_score
      3. UPSERT assistant_evaluation 记录
      4. 上报 Prometheus 指标
    """

    def __init__(self, session: AsyncSession):
        self.session = session

    async def calculate_and_save(
        self,
        case_id: str,
        conversation_id: str | None,
        close_reason: str | None,
        user_rating: int | None = None,
        trace_id: str | None = None,
    ) -> int:
        """
        计算并保存质量评分

        Args:
            case_id: 工单ID
            conversation_id: 对话ID
            close_reason: 关闭原因
            user_rating: 用户评分（1-5），可选
            trace_id: 调用链ID

        Returns:
            int: composite_score (0-100)
        """
        # 1. 读取 case 基础信息（使用原始 SQL，eval-service 无 Conversation ORM 模型）
        from sqlalchemy import text as _text

        conv_result = await self.session.execute(
            _text(
                """
                SELECT message_count, repeat_question_count, started_at, ended_at
                FROM conversation
                WHERE case_id = :case_id
                ORDER BY started_at DESC
                LIMIT 1
                """
            ),
            {"case_id": case_id},
        )
        conv_row = conv_result.fetchone()

        if conv_row:
            message_count = conv_row[0] or 0
            repeat_question_count = conv_row[1] or 0
            started_at = conv_row[2]
            ended_at = conv_row[3]
            if ended_at:
                session_duration = int((ended_at - started_at).total_seconds())
            else:
                session_duration = int((datetime.now(UTC) - started_at).total_seconds())
        else:
            message_count = 0
            repeat_question_count = 0
            session_duration = 0

        # 2. 读取已有 evaluation（获取可能已有的评分）
        existing_rating = await self._get_existing_rating(case_id)
        if user_rating is None and existing_rating is not None:
            user_rating = existing_rating

        # 3. 读取 prompt_audit 元数据（确保 ai_quality 维度始终参与评分）
        # 使用原始 SQL 查询，无需在 conversation-service 中定义 PromptAudit ORM 模型

        audit_result = await self.session.execute(
            text(
                """
                SELECT has_sop, kb_chunks_count, kb_top_score
                FROM prompt_audit
                WHERE case_id = :case_id
                ORDER BY captured_at DESC
                LIMIT 1
                """
            ),
            {"case_id": case_id},
        )
        audit_row = audit_result.fetchone()
        has_sop = audit_row[0] if audit_row else None
        kb_chunks_count = audit_row[1] if audit_row else None
        kb_top_score = audit_row[2] if audit_row else None

        # 4. 组装信号
        signals = QualitySignals(
            case_id=case_id,
            close_reason=close_reason,
            session_duration_sec=session_duration if session_duration > 0 else None,
            message_count=message_count if message_count > 0 else None,
            repeat_question_count=repeat_question_count,
            user_rating=user_rating,
            has_sop=has_sop,
            kb_chunks_count=kb_chunks_count,
            kb_top_score=kb_top_score,
        )

        # 4. 计算综合评分
        quality = compute_quality_score(signals)

        # 5. UPSERT assistant_evaluation
        await self._upsert_evaluation(
            case_id=case_id,
            conversation_id=conversation_id,
            close_reason=close_reason,
            session_duration=session_duration if session_duration > 0 else None,
            message_count=message_count if message_count > 0 else None,
            repeat_question_count=repeat_question_count,
            user_rating=user_rating,
            quality=quality,
            trace_id=trace_id,
        )

        # 6. 记录日志
        logger.info(
            event="quality_score_computed",
            message=f"综合质量评分已计算: case_id={case_id}, score={quality.composite_score}",
            case_id=case_id,
            composite_score=quality.composite_score,
            close_reason=close_reason,
            rating_included=quality.rating_included,
            trace_id=trace_id,
        )

        return quality.composite_score

    async def update_user_rating(
        self,
        case_id: str,
        conversation_id: str | None,
        user_rating: int,
        trace_id: str | None = None,
    ) -> int:
        """
        更新用户评分并重新计算综合评分

        Args:
            case_id: 工单ID
            conversation_id: 对话ID
            user_rating: 用户评分（1-5）
            trace_id: 调用链ID

        Returns:
            int: 更新后的 composite_score
        """
        # 获取现有的 close_reason 等信息
        existing = await self._get_evaluation(case_id)
        close_reason = existing.get("close_reason") if existing else None

        return await self.calculate_and_save(
            case_id=case_id,
            conversation_id=conversation_id,
            close_reason=close_reason,
            user_rating=user_rating,
            trace_id=trace_id,
        )

    async def _get_existing_rating(self, case_id: str) -> int | None:
        """获取已存在的用户评分"""

        result = await self.session.execute(
            text(
                """
                SELECT score
                FROM assistant_evaluation
                WHERE case_id = :case_id
                ORDER BY created_at DESC
                LIMIT 1
                """
            ),
            {"case_id": case_id},
        )
        row = result.fetchone()
        return row[0] if row and row[0] else None

    async def _get_evaluation(self, case_id: str) -> dict[str, Any] | None:
        """获取评价记录"""

        result = await self.session.execute(
            text(
                """
                SELECT evaluation_id, score, close_reason, composite_score
                FROM assistant_evaluation
                WHERE case_id = :case_id
                ORDER BY created_at DESC
                LIMIT 1
                """
            ),
            {"case_id": case_id},
        )
        row = result.fetchone()
        if row:
            return {
                "evaluation_id": row[0],
                "score": row[1],
                "close_reason": row[2],
                "composite_score": row[3],
            }
        return None

    async def _upsert_evaluation(
        self,
        case_id: str,
        conversation_id: str | None,
        close_reason: str | None,
        session_duration: int | None,
        message_count: int | None,
        repeat_question_count: int,
        user_rating: int | None,
        quality: QualityScore,
        trace_id: str | None,
    ) -> None:
        """UPSERT assistant_evaluation 记录"""

        # 先查询最新一条评价记录，避免依赖 case_id 的唯一约束
        existing = await self._get_evaluation(case_id)
        params = {
            "case_id": case_id,
            "conversation_id": conversation_id,
            "score": user_rating,
            "close_reason": close_reason,
            "session_duration": session_duration,
            "message_count": message_count,
            "repeat_question_count": repeat_question_count,
            "composite_score": quality.composite_score,
            "score_breakdown": json.dumps(quality.breakdown, ensure_ascii=False),
            "trace_id": trace_id,
        }

        if existing:
            await self.session.execute(
                text(
                    """
                    UPDATE assistant_evaluation
                    SET score = :score,
                        close_reason = :close_reason,
                        session_duration_sec = :session_duration,
                        message_count = :message_count,
                        repeat_question_count = :repeat_question_count,
                        composite_score = :composite_score,
                        score_breakdown = CAST(:score_breakdown AS JSONB),
                        trace_id = :trace_id
                    WHERE evaluation_id = :evaluation_id
                    """
                ),
                {
                    **params,
                    "evaluation_id": existing["evaluation_id"],
                },
            )
        else:
            await self.session.execute(
                text(
                    """
                    INSERT INTO assistant_evaluation (
                        case_id, conversation_id, assistant_type, score,
                        close_reason, session_duration_sec, message_count,
                        repeat_question_count, composite_score, score_breakdown,
                        trace_id, created_at
                    ) VALUES (
                        :case_id, :conversation_id, 'openclaw', :score,
                        :close_reason, :session_duration, :message_count,
                        :repeat_question_count, :composite_score, CAST(:score_breakdown AS JSONB),
                        :trace_id, NOW()
                    )
                    """
                ),
                params,
            )
        await self.session.commit()


class QualityStatsService:
    """质量统计服务（Admin 接口使用）"""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_quality_stats(self, days: int = 7) -> dict[str, Any]:
        """
        获取质量统计信息

        Args:
            days: 统计天数，默认 7 天

        Returns:
            dict: 包含平均综合分、关闭原因分布等
        """

        # 近 N 天平均 composite_score
        result = await self.session.execute(
            text(
                """
                SELECT
                    AVG(composite_score) as avg_score,
                    COUNT(*) as total_count,
                    COUNT(CASE WHEN composite_score <= 40 THEN 1 END) as low_score_count
                FROM assistant_evaluation
                WHERE created_at >= NOW() - (:days * INTERVAL '1 day')
                AND composite_score IS NOT NULL
                """
            ),
            {"days": days},
        )
        row = result.fetchone()
        avg_score = round(row[0], 2) if row[0] else 0.0
        total_count = row[1] or 0
        low_score_count = row[2] or 0

        # 关闭原因分布
        reason_result = await self.session.execute(
            text(
                """
                SELECT
                    COALESCE(close_reason, 'unknown') as reason,
                    COUNT(*) as count
                FROM assistant_evaluation
                WHERE created_at >= NOW() - (:days * INTERVAL '1 day')
                GROUP BY close_reason
                ORDER BY count DESC
                """
            ),
            {"days": days},
        )
        close_reason_dist = {row[0]: row[1] for row in reason_result.fetchall()}

        # 用户评分分布
        rating_result = await self.session.execute(
            text(
                """
                SELECT
                    COALESCE(score::text, 'unrated') as rating,
                    COUNT(*) as count
                FROM assistant_evaluation
                WHERE created_at >= NOW() - (:days * INTERVAL '1 day')
                GROUP BY score
                ORDER BY count DESC
                """
            ),
            {"days": days},
        )
        rating_dist = {row[0]: row[1] for row in rating_result.fetchall()}

        return {
            "period_days": days,
            "average_composite_score": avg_score,
            "total_evaluated_cases": total_count,
            "low_score_cases": low_score_count,
            "low_score_rate": round(low_score_count / total_count * 100, 2) if total_count > 0 else 0.0,
            "close_reason_distribution": close_reason_dist,
            "user_rating_distribution": rating_dist,
        }

    async def get_low_score_cases(
        self,
        min_score: int = 0,
        max_score: int = 39,
        limit: int = 20,
        offset: int = 0,
    ) -> dict[str, Any]:
        """
        获取低分工单列表

        Args:
            min_score: 最低分数（包含）
            max_score: 最高分数（包含）
            limit: 每页数量
            offset: 偏移量

        Returns:
            dict: 包含工单列表和分页信息
        """

        # 查询低分工件列表
        result = await self.session.execute(
            text(
                """
                SELECT
                    e.case_id,
                    e.conversation_id,
                    e.score as user_rating,
                    e.composite_score,
                    e.close_reason,
                    e.score_breakdown,
                    e.created_at as evaluated_at,
                    c.title as case_title,
                    c.status as case_status
                FROM assistant_evaluation e
                LEFT JOIN "case" c ON e.case_id = c.case_id
                WHERE e.composite_score BETWEEN :min_score AND :max_score
                ORDER BY e.composite_score ASC, e.created_at DESC
                LIMIT :limit OFFSET :offset
                """
            ),
            {"min_score": min_score, "max_score": max_score, "limit": limit, "offset": offset},
        )

        cases = []
        for row in result.fetchall():
            cases.append(
                {
                    "case_id": row[0],
                    "conversation_id": str(row[1]) if row[1] else None,
                    "user_rating": row[2],
                    "composite_score": row[3],
                    "close_reason": row[4],
                    "score_breakdown": row[5],
                    "evaluated_at": row[6].isoformat() if row[6] else None,
                    "case_title": row[7],
                    "case_status": row[8],
                }
            )

        # 查询总数
        count_result = await self.session.execute(
            text(
                """
                SELECT COUNT(*) FROM assistant_evaluation
                WHERE composite_score BETWEEN :min_score AND :max_score
                """
            ),
            {"min_score": min_score, "max_score": max_score},
        )
        total = count_result.scalar() or 0

        return {
            "items": cases,
            "total": total,
            "limit": limit,
            "offset": offset,
        }
