"""
Case Service - 业务逻辑层
"""

from typing import List, Optional
from datetime import datetime

from ..models.case import Case, CaseStatus
from ..repositories.case_repo import CaseRepository

import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../..')))

from shared.models.schemas import CaseCreate, CaseResponse, CaseListResponse, CaseStatsResponse, ClientListResponse, ClientInfo
from shared.utils.logger import get_logger
from shared.utils.otel import get_current_trace_id

logger = get_logger("case-service")

class CaseService:
    """工单业务服务"""
    
    def __init__(self, repository: CaseRepository):
        self.repository = repository
    
    def _generate_case_id(self) -> str:
        """生成工单ID: Q + YYYYMMDD + 5位序号"""
        from datetime import datetime
        import random
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

        case = Case(
            case_id=case_id,
            user_id=user.user_id,
            client_id=case_create.client_id,
            title=case_create.title,
            description=case_create.description,
            status=CaseStatus.created,
            assistant_type=case_create.assistant_type or "openclaw",
            trace_id=trace_id
        )
        
        created_case = await self.repository.create(case)
        
        logger.info(
            event="case_created",
            message=f"Created case {case_id}",
            case_id=case_id,
            client_id=case_create.client_id
        )
        
        return CaseResponse.model_validate(created_case)
    
    async def get_case(self, case_id: str) -> Optional[CaseResponse]:
        """获取工单详情"""
        case = await self.repository.get_by_id(case_id)
        if not case:
            return None
        return CaseResponse.model_validate(case)
    
    async def list_cases(self, client_id: str) -> List[CaseResponse]:
        """获取客户端的所有工单"""
        cases = await self.repository.get_by_client_id(client_id)
        return [CaseResponse.model_validate(case) for case in cases]
    
    async def confirm_case(
        self, 
        case_id: str
    ) -> Optional[CaseResponse]:
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
    ) -> Optional[CaseResponse]:
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
        status: Optional[str] = None,
        client_id: Optional[str] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
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

    async def get_client_list(self) -> ClientListResponse:
        """获取客户端列表（Admin）"""
        rows = await self.repository.get_client_stats()
        items = [ClientInfo(**r) for r in rows]
        return ClientListResponse(items=items, total=len(items))
