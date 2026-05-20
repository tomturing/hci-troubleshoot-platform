"""
Case Service - 业务逻辑层
"""

from datetime import datetime

from shared.models.schemas import (
    CaseCreate,
    CaseListResponse,
    CaseResponse,
    CaseStatsResponse,
    ClientInfo,
    ClientListResponse,
)
from shared.observability.logger import get_logger
from shared.observability.otel import get_current_trace_id

from ..models.case import Case, CaseStatus
from ..repositories.case_repo import CaseRepository

logger = get_logger("case-service")

# 分类关键词映射表（用于自动推断工单分类）
_CATEGORY_KEYWORDS = {
    "vm": ["虚拟机", "VM", "vm", "开机", "关机", "迁移", "快照", "虚机"],
    "storage": ["存储", "磁盘", "卷", "NFS", "iSCSI", "ASAN", "硬盘"],
    "network": ["网络", "IP", "VLAN", "连通", "丢包", "网口", "网卡", "vxlan"],
    "cluster": ["集群", "节点", "宿主机", "host", "主机", "集群管理"],
    "backup": ["备份", "恢复", "快照", "克隆", "容灾"],
    "hardware": ["硬件", "IPMI", "风扇", "电源", "温度", "磁盘灯"],
}


def _infer_category(title: str, description: str | None) -> str | None:
    """
    基于标题和描述关键词推断工单分类。

    Args:
        title: 工单标题
        description: 工单描述（可选）

    Returns:
        推断的分类字符串，无法匹配时返回 None
    """
    text = (title or "") + " " + (description or "")
    for category, keywords in _CATEGORY_KEYWORDS.items():
        if any(kw in text for kw in keywords):
            return category
    return None


class CaseService:
    """工单业务服务"""

    def __init__(self, repository: CaseRepository):
        self.repository = repository

    def _generate_case_id(self) -> str:
        """生成工单ID: Q + YYYYMMDD + 5位序号"""
        import random
        from datetime import datetime
        date_str = datetime.utcnow().strftime("%Y%m%d")
        seq = str(random.randint(0, 99999)).zfill(5)
        return f"Q{date_str}{seq}"

    async def create_case(
        self,
        case_create: CaseCreate
    ) -> CaseResponse:
        """创建新工单"""
        case_id = self._generate_case_id()
        trace_id = get_current_trace_id()

        # 获取或创建用户
        from shared.models.user import User
        user = await self.repository.get_user_by_client_id(case_create.client_id)
        if not user:
            user = User(
                client_id=case_create.client_id,
                user_type="temporary",
                trace_id=trace_id
            )
            user = await self.repository.create_user(user)
            logger.info(f"Created new user for client_id: {case_create.client_id}")

        # 工单分类由 S0 意图识别流程自动写入（PR #88），此处 category 初始为 None
        # _infer_category 保留供后续兜底使用，暂不在创建工单时调用
        category = _infer_category(case_create.title, case_create.description)
        if category:
            logger.info(
                event="category_auto_inferred",
                case_id=case_id,
                inferred_category=category,
            )

        case = Case(
            case_id=case_id,
            user_id=user.user_id,
            client_id=case_create.client_id,
            title=case_create.title,
            description=case_create.description,
            status=CaseStatus.created,
            assistant_type=case_create.assistant_type or "openclaw",
            category=category,
            trace_id=trace_id
        )

        created_case = await self.repository.create(case)

        logger.info(
            event="case_created",
            message=f"Created case {case_id}",
            case_id=case_id,
            client_id=case_create.client_id,
            category=category,
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

    async def confirm_case(
        self,
        case_id: str
    ) -> CaseResponse | None:
        """确认工单"""
        case = await self.repository.update_status(
            case_id,
            CaseStatus.confirmed
        )
        if not case:
            return None

        logger.info(
            event="case_confirmed",
            message=f"Confirmed case {case_id}",
            case_id=case_id
        )

        return CaseResponse.model_validate(case)

    async def close_case(
        self,
        case_id: str
    ) -> CaseResponse | None:
        """关闭工单"""
        case = await self.repository.update_status(
            case_id,
            CaseStatus.closed
        )
        if not case:
            return None

        logger.info(
            event="case_closed",
            message=f"Closed case {case_id}",
            case_id=case_id
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
            skip=skip, limit=limit,
            status=status, client_id=client_id,
            start_time=start_time, end_time=end_time,
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

    async def escalate_to_human(
        self,
        case_id: str,
        close_reason: str = "s0_classification_failed",
    ) -> CaseResponse | None:
        """
        将工单直接跳转到 in_progress 状态，移交人工处理。

        适用于以下场景：
          - S0 意图识别彻底失败（close_reason="s0_classification_failed"）
          - S6 用户选 C 升级人工（close_reason="escalated"，由 handle_s6_resolution_choice 调用）

        与 confirm_case() 不同，此方法允许 created → in_progress 直接跳转，
        跳过 confirmed 中间态（S0 失败时 AI 未完成分类，不应写 confirmed）。

        Args:
            case_id: 工单ID
            close_reason: 关闭原因，默认 s0_classification_failed

        Returns:
            更新后的 CaseResponse，或 None（工单不存在）
        """
        case = await self.repository.update_status(
            case_id,
            CaseStatus.in_progress,
            close_reason=close_reason,
        )
        if not case:
            return None

        logger.info(
            event="case_escalated_to_human",
            message=f"工单 {case_id} 已移交人工（{close_reason}）",
            case_id=case_id,
            close_reason=close_reason,
        )
        return CaseResponse.model_validate(case)

    async def get_client_list(self) -> ClientListResponse:
        """获取客户端列表（Admin）"""
        rows = await self.repository.get_client_stats()
        items = [ClientInfo(**r) for r in rows]
        return ClientListResponse(items=items, total=len(items))
