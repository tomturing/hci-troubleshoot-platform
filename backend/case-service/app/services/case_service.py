"""
Case Service - 业务逻辑层
"""

from datetime import UTC, datetime

from shared.models.schemas import (
    CaseCreate,
    CaseListResponse,
    CaseResponse,
    CaseStatsResponse,
    ClientInfo,
    ClientListResponse,
    CloseReason,
)
from shared.utils.logger import get_logger
from shared.utils.otel import get_current_trace_id

from app.config import settings

from ..models.case import Case, CaseStatus
from ..repositories.case_repo import CaseRepository
from .quality_score import QualityScoreService

logger = get_logger("case-service")


class CaseService:
    """工单业务服务"""

    def __init__(self, repository: CaseRepository):
        self.repository = repository

    async def create_case(self, case_create: CaseCreate) -> CaseResponse:
        """创建新工单"""
        case_id = await self.repository.generate_case_id()
        trace_id = get_current_trace_id()

        # 获取或创建用户
        from shared.models.user import User

        user = await self.repository.get_user_by_client_id(case_create.client_id)
        if not user:
            user = User(client_id=case_create.client_id, user_type="temporary", trace_id=trace_id)
            user = await self.repository.create_user(user)
            logger.info(f"Created new user for client_id: {case_create.client_id}")

        case = Case(
            case_id=case_id,
            user_id=user.user_id,
            client_id=case_create.client_id,
            title=case_create.title,
            description=case_create.description,
            status=CaseStatus.created,
            assistant_type=case_create.assistant_type or "openclaw",
            trace_id=trace_id,
        )

        created_case = await self.repository.create(case)

        logger.info(
            event="case_created", message=f"Created case {case_id}", case_id=case_id, client_id=case_create.client_id
        )

        return CaseResponse.model_validate(created_case)

    async def get_case(self, case_id: str) -> CaseResponse | None:
        """获取工单详情"""
        case = await self.repository.get_by_id(case_id)
        if not case:
            return None
        return CaseResponse.model_validate(case)

    async def list_cases(self, client_id: str) -> list[CaseResponse]:
        """获取客户端的所有工单"""
        cases = await self.repository.get_by_client_id(client_id)
        return [CaseResponse.model_validate(case) for case in cases]

    async def confirm_case(self, case_id: str) -> CaseResponse | None:
        """确认工单"""
        case = await self.repository.update_status(case_id, CaseStatus.confirmed)
        if not case:
            return None

        logger.info(event="case_confirmed", message=f"Confirmed case {case_id}", case_id=case_id)

        return CaseResponse.model_validate(case)

    async def close_case(
        self,
        case_id: str,
        close_reason: CloseReason | None = None,
    ) -> CaseResponse | None:
        """关闭工单，并异步推送摘要至 KB Service 进行知识沉淀

        Args:
            case_id: 工单ID
            close_reason: 关闭原因（user_command/timeout/abandon/admin_close）

        Returns:
            CaseResponse: 关闭后的工单信息，工单不存在返回 None
        """
        trace_id = get_current_trace_id()

        # 1. 更新工单状态为关闭
        case = await self.repository.update_status(case_id, CaseStatus.closed)
        if not case:
            return None

        # 2. 如果提供了关闭原因，写入 case 表
        if close_reason:
            case = await self.repository.update_close_reason(case_id, close_reason.value)
            logger.info(
                event="case_close_reason_recorded",
                message=f"工单 {case_id} 关闭原因已记录: {close_reason.value}",
                case_id=case_id,
                close_reason=close_reason.value,
                trace_id=trace_id,
            )

        logger.info(
            event="case_closed",
            message=f"工单 {case_id} 已关闭",
            case_id=case_id,
            close_reason=close_reason.value if close_reason else None,
            trace_id=trace_id,
        )

        # 3. 调用 QualityScoreService 计算并保存质量评分
        if close_reason:
            try:
                # 使用当前 session 的 session_factory 创建 QualityScoreService
                from shared.database.postgres import database_manager

                quality_service = QualityScoreService(database_manager.get_session)
                composite_score = await quality_service.calculate_and_save(
                    case_id=case_id,
                    close_reason=close_reason.value,
                    trace_id=trace_id,
                )
                logger.info(
                    event="quality_score_triggered",
                    message=f"工单 {case_id} 质量评分已触发计算",
                    case_id=case_id,
                    composite_score=composite_score,
                    trace_id=trace_id,
                )
            except Exception as e:
                # 质量评分失败不应影响关闭流程
                logger.error(
                    event="quality_score_trigger_failed",
                    message=f"工单 {case_id} 质量评分触发失败: {e}",
                    case_id=case_id,
                    error=str(e),
                    trace_id=trace_id,
                )

        # 4. 异步 fire-and-forget 推送至 KB Service（不阻塞主流程）
        if settings.KB_PUSH_ENABLED:
            from .kb_pusher import fire_and_forget_push

            fire_and_forget_push(
                kb_service_url=settings.KB_SERVICE_URL,
                internal_token=settings.INTERNAL_API_TOKEN,
                case_id=case_id,
                title=case.title or case_id,
                description=case.description or "",
            )

        return CaseResponse.model_validate(case)

    # ============ Admin API ============

    async def list_all_cases(
        self,
        skip: int = 0,
        limit: int = 20,
        status: str | None = None,
        client_id: str | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
    ) -> CaseListResponse:
        """获取所有工单（Admin: 分页 + 筛选）"""
        items, total = await self.repository.get_all(
            skip=skip,
            limit=limit,
            status=status,
            client_id=client_id,
            start_time=start_time,
            end_time=end_time,
        )
        return CaseListResponse(
            items=[CaseResponse.model_validate(c) for c in items],
            total=total,
            skip=skip,
            limit=limit,
        )

    async def get_case_stats(self) -> CaseStatsResponse:
        """获取工单统计（Admin）"""
        by_status = await self.repository.count_by_status()
        total = sum(by_status.values())
        return CaseStatsResponse(total=total, by_status=by_status)

    async def get_client_list(self) -> ClientListResponse:
        """获取客户端列表（Admin）"""
        rows = await self.repository.get_client_stats()
        items = [ClientInfo(**r) for r in rows]
        return ClientListResponse(items=items, total=len(items))
