"""
Quality Score Service - 综合质量评分服务

触发时机：case close（case-service 的 close_case() 调用后）
职责：
  1. 采集所有可用的质量信号
  2. 计算 composite_score
  3. UPSERT assistant_evaluation 记录
  4. 上报 Prometheus 指标
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from prometheus_client import Counter, Gauge, Histogram
from shared.utils.logger import get_logger
from shared.utils.otel import get_current_trace_id
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import AssistantEvaluation, Case, Conversation, PromptAudit

logger = get_logger("quality-score-service")

# ─────────────────────────────────────────────────────────────
# Prometheus 指标定义
# ─────────────────────────────────────────────────────────────

# 综合质量分 Histogram（分布统计）
QUALITY_SCORE_HISTOGRAM = Histogram(
    "hci_case_quality_score",
    "Case quality composite score distribution",
    buckets=[0, 20, 40, 60, 70, 80, 90, 100],
)

# 综合质量分 Gauge（按 close_reason 分类，用于实时监控）
QUALITY_SCORE_GAUGE = Gauge(
    "hci_case_quality_score_current",
    "工单综合质量分当前值（0-100）",
    labelnames=["close_reason"],
)

# 关闭原因计数器
CLOSE_REASON_COUNTER = Counter(
    "hci_case_close_reason_total",
    "Case close reason distribution",
    labelnames=["reason"],
)

# 用户评分分布计数器
USER_RATING_COUNTER = Counter(
    "hci_user_rating_total",
    "用户显式评分分布",
    labelnames=["score"],
)

# 关闭原因评分映射
CLOSE_REASON_SCORE = {
    "user_command": 100,  # 主动解决：最高分
    "timeout": 50,        # 超时：中性基础分
    "admin_close": 50,    # 人工干预：略高于中性
    "abandon": 10,        # 放弃：最低分
}

# 各维度基础权重（归一化前）
BASE_WEIGHTS = {
    "user_rating": 0.30,   # 用户满意度（无评分时降为 0）
    "close_intent": 0.20,  # 关闭意图
    "efficiency": 0.20,    # 解决效率
    "repeat_penalty": 0.15,  # 用户重复提问（负向信号）
    "ai_quality": 0.15,    # AI 能力质量
}


@dataclass
class QualitySignals:
    """质量信号数据类

    包含计算综合质量分所需的所有信号数据
    """
    case_id: str
    close_reason: str | None = None           # "user_command" | "timeout" | "abandon" | "admin_close" | None
    session_duration_sec: int | None = None
    message_count: int | None = None
    repeat_question_count: int = 0               # 相似重复提问次数，conversation-service 统计，始终有值
    user_rating: int | None = None            # 1–5，用户未评分则 None
    has_sop: bool | None = None               # 来自 prompt_audit 元数据，100% 覆盖
    kb_chunks_count: int | None = None        # 来自 prompt_audit 元数据，100% 覆盖
    kb_top_score: float | None = None         # 来自 prompt_audit 元数据，100% 覆盖


@dataclass
class QualityScore:
    """质量评分结果数据类"""
    composite_score: int      # 0–100 最终综合分
    rating_included: bool     # 是否含用户评分维度
    breakdown: dict           # 各维度详细分解


def _duration_score(session_duration_sec: int | None) -> int:
    """根据会话时长计算效率分

    时长分规则：
    - < 5 min   → 100  (快速解决)
    - 5–15 min  → 80
    - 15–30 min → 60
    - 30–60 min → 40
    - > 60 min  → 20   (长时间未解决)
    """
    if session_duration_sec is None:
        return 50  # 无数据时返回中性分

    minutes = session_duration_sec / 60

    if minutes < 5:
        return 100
    elif minutes < 15:
        return 80
    elif minutes < 30:
        return 60
    elif minutes < 60:
        return 40
    else:
        return 20


def _message_count_score(message_count: int | None) -> int:
    """根据消息轮数计算效率分

    消息轮数分规则：
    - ≤ 3 轮    → 100  (一问即答)
    - 4–8 轮    → 80
    - 9–15 轮   → 60
    - 16–25 轮  → 40
    - > 25 轮   → 20   (反复沟通)
    """
    if message_count is None:
        return 50  # 无数据时返回中性分

    if message_count <= 3:
        return 100
    elif message_count <= 8:
        return 80
    elif message_count <= 15:
        return 60
    elif message_count <= 25:
        return 40
    else:
        return 20


def _timeout_score(session_duration_sec: int) -> int:
    """timeout 关闭原因的特殊评分逻辑

    timeout 时结合 session_duration_sec 做修正：
    - > 1 小时   → 70 (很可能已解决)
    - 30-60 分钟 → 55
    - < 30 分钟  → 40 (可能放弃)
    """
    if session_duration_sec > 3600:   # > 1 小时才超时
        return 70
    elif session_duration_sec > 1800:  # 30–60 分钟
        return 55
    else:
        return 40


def _repeat_question_score(repeat_count: int) -> int:
    """重复提问评分

    重复提问次数越多，说明 AI 回答越无效：
    - 0 次重复  → 100  (无重复，AI 一次回答即解决)
    - 1 次重复  → 70   (轻微重复，可接受)
    - 2 次重复  → 45   (明显重复，AI 回答有问题)
    - 3 次重复  → 20   (严重重复，AI 基本无效)
    - ≥ 4 次重复 → 0    (完全失败)
    """
    mapping = {
        0: 100,
        1: 70,
        2: 45,
        3: 20,
    }
    return mapping.get(repeat_count, 0)


def _ai_quality_score(has_sop: bool, kb_chunks_count: int, kb_top_score: float) -> int:
    """AI 能力质量分

    - SOP 命中 +40 基础分
    - KB 命中质量（相关度加权）：最高 60 分
    """
    score = 0
    # SOP 命中 +40 基础分
    if has_sop:
        score += 40
    # KB 命中质量（相关度加权）
    if kb_chunks_count > 0:
        score += min(60, int(kb_top_score * 60))  # 最高 60 分
    return min(100, score)


def compute_quality_score(s: QualitySignals) -> QualityScore:
    """计算综合质量分

    根据设计文档的评分算法，综合各维度信号计算最终质量分。
    权重在无用户评分时自动归一化（各维度原始权重等比放大至总和 100%）。

    Args:
        s: 质量信号数据

    Returns:
        QualityScore: 包含综合分、是否包含用户评分、各维度分解的结果
    """
    raw_weights = dict(BASE_WEIGHTS)
    dim_scores = {}

    # 维度 2：关闭意图（始终参与）
    close_score = CLOSE_REASON_SCORE.get(s.close_reason, 50)
    if s.close_reason == "timeout" and s.session_duration_sec:
        close_score = _timeout_score(s.session_duration_sec)
    dim_scores["close_intent"] = close_score

    # 维度 3：解决效率（始终参与）
    duration_s = _duration_score(s.session_duration_sec)
    msg_s = _message_count_score(s.message_count)
    dim_scores["efficiency"] = (duration_s + msg_s) // 2

    # 维度 4：用户重复提问（始终参与，100% 覆盖）
    dim_scores["repeat_penalty"] = _repeat_question_score(s.repeat_question_count)

    # 维度 5：AI 能力质量（prompt_audit 元数据，100% 覆盖；极少情况下为 None 则跳过）
    if s.has_sop is not None and s.kb_chunks_count is not None:
        dim_scores["ai_quality"] = _ai_quality_score(
            s.has_sop, s.kb_chunks_count, s.kb_top_score or 0.0
        )
    else:
        raw_weights.pop("ai_quality")  # 无数据时权重转移到其他维度

    # 维度 1：用户满意度（仅有评分时参与）
    if s.user_rating is not None:
        dim_scores["user_rating"] = int((s.user_rating - 1) / 4 * 100)
    else:
        raw_weights.pop("user_rating")  # 无评分时权重归零，其余维度等比放大

    # 归一化加权平均（确保权重之和 = 1.0）
    active_dims = list(dim_scores.keys())
    total_weight = sum(raw_weights[k] for k in active_dims)
    composite = sum(dim_scores[k] * raw_weights[k] for k in active_dims) / total_weight

    return QualityScore(
        composite_score=round(composite),
        rating_included=s.user_rating is not None,
        breakdown={k: round(dim_scores[k]) for k in active_dims},
    )


class QualityScoreService:
    """综合质量评分服务

    触发时机：case close（case-service 的 close_case() 调用后）
    职责：
      1. 采集所有可用的质量信号
      2. 计算 composite_score
      3. UPSERT assistant_evaluation 记录
      4. 上报 Prometheus 指标
    """

    def __init__(self, session: AsyncSession):
        """初始化服务

        Args:
            session: SQLAlchemy 异步会话
        """
        self.session = session

    async def calculate_and_save(
        self,
        case_id: str,
        close_reason: str | None,
        trace_id: str | None = None,
    ) -> int | None:
        """在 case close 时调用，计算并保存综合质量分

        Args:
            case_id: 工单ID
            close_reason: 关闭原因
            trace_id: 调用链ID

        Returns:
            composite_score: 综合质量分 0-100，计算失败返回 None
        """
        trace_id = trace_id or get_current_trace_id()

        try:
            # 1. 读取 case 基础信息
            case = await self._get_case(case_id)
            if not case:
                logger.warning(event="case_not_found", case_id=case_id, trace_id=trace_id)
                return None

            # 计算会话时长
            session_duration_sec = 0
            if case.closed_at and case.created_at:
                session_duration_sec = int((case.closed_at - case.created_at).total_seconds())

            # 2. 读取 conversation 消息数 + 重复提问数
            conv = await self._get_conversation(case_id)
            message_count = conv.message_count if conv else 0
            repeat_question_count = conv.repeat_question_count if conv and conv.repeat_question_count else 0

            # 3. 读取最近一条 prompt_audit 元数据（如有）
            audit = await self._get_latest_prompt_audit(case_id)

            # 4. 读取已有的 assistant_evaluation（用户可能已评分）
            existing_eval = await self._get_evaluation(case_id)
            user_rating = existing_eval.score if existing_eval else None

            # 5. 组装信号
            signals = QualitySignals(
                case_id=case_id,
                close_reason=close_reason,
                session_duration_sec=session_duration_sec,
                message_count=message_count,
                repeat_question_count=repeat_question_count,
                user_rating=user_rating,
                has_sop=audit.has_sop if audit else None,
                kb_chunks_count=audit.kb_chunks_count if audit else None,
                kb_top_score=audit.kb_top_score if audit else None,
            )

            # 6. 计算
            quality = compute_quality_score(signals)

            # 7. UPSERT assistant_evaluation
            await self._upsert_evaluation(
                case_id=case_id,
                close_reason=close_reason,
                session_duration_sec=session_duration_sec,
                message_count=message_count,
                repeat_question_count=repeat_question_count,
                quality=quality,
                assistant_type=case.assistant_type,
                conversation_id=conv.conversation_id if conv else None,
                trace_id=trace_id,
            )

            # 8. Prometheus 指标上报
            reason_label = close_reason or "unknown"
            QUALITY_SCORE_GAUGE.labels(close_reason=reason_label).set(quality.composite_score)
            QUALITY_SCORE_HISTOGRAM.observe(quality.composite_score)
            CLOSE_REASON_COUNTER.labels(reason=reason_label).inc()

            if user_rating is not None:
                USER_RATING_COUNTER.labels(score=str(user_rating)).inc()

            logger.info(
                event="quality_score_computed",
                case_id=case_id,
                composite_score=quality.composite_score,
                close_reason=close_reason,
                rating_included=quality.rating_included,
                breakdown=quality.breakdown,
                trace_id=trace_id,
            )

            return quality.composite_score

        except Exception as e:
            logger.error(
                event="quality_score_calculation_failed",
                message=f"工单 {case_id} 质量评分计算失败: {e}",
                case_id=case_id,
                close_reason=close_reason,
                error=str(e),
                trace_id=trace_id,
            )
            return None

    async def _get_case(self, case_id: str) -> Case | None:
        """获取工单信息"""
        result = await self.session.execute(select(Case).where(Case.case_id == case_id))
        return result.scalar_one_or_none()

    async def _get_conversation(self, case_id: str) -> Conversation | None:
        """获取工单关联的对话会话（取最新一条）"""
        result = await self.session.execute(
            select(Conversation)
            .where(Conversation.case_id == case_id)
            .order_by(Conversation.started_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def _get_latest_prompt_audit(self, case_id: str) -> PromptAudit | None:
        """获取工单关联的最新 prompt_audit 记录"""
        result = await self.session.execute(
            select(PromptAudit)
            .where(PromptAudit.case_id == case_id)
            .order_by(PromptAudit.captured_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def _get_evaluation(self, case_id: str) -> AssistantEvaluation | None:
        """获取工单关联的评价记录"""
        result = await self.session.execute(
            select(AssistantEvaluation)
            .where(AssistantEvaluation.case_id == case_id)
            .order_by(AssistantEvaluation.created_at.desc())
            .limit(1)
        )
        return result.scalars().first()

    async def _upsert_evaluation(
        self,
        case_id: str,
        close_reason: str | None,
        session_duration_sec: int,
        message_count: int,
        repeat_question_count: int,
        quality: QualityScore,
        assistant_type: str,
        conversation_id: uuid.UUID | None,
        trace_id: str,
    ) -> AssistantEvaluation:
        """UPSERT assistant_evaluation 记录"""
        existing = await self._get_evaluation(case_id)

        now = datetime.now(UTC)

        if existing:
            # 更新已有记录
            existing.close_reason = close_reason
            existing.session_duration_sec = session_duration_sec
            existing.message_count = message_count
            existing.repeat_question_count = repeat_question_count
            existing.composite_score = quality.composite_score
            existing.score_breakdown = quality.breakdown
            existing.calculated_at = now
            existing.conversation_id = conversation_id
            existing.trace_id = trace_id

            await self.session.flush()
            await self.session.refresh(existing)
            return existing
        else:
            # 创建新记录
            new_eval = AssistantEvaluation(
                case_id=case_id,
                conversation_id=conversation_id,
                assistant_type=assistant_type,
                close_reason=close_reason,
                session_duration_sec=session_duration_sec,
                message_count=message_count,
                repeat_question_count=repeat_question_count,
                composite_score=quality.composite_score,
                score_breakdown=quality.breakdown,
                calculated_at=now,
                trace_id=trace_id,
            )

            self.session.add(new_eval)
            await self.session.flush()
            await self.session.refresh(new_eval)
            return new_eval
