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

from shared.models.schemas import CaseCreate, CaseResponse
from shared.utils.logger import get_logger

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
        case_create: CaseCreate,
        trace_id: Optional[str] = None
    ) -> CaseResponse:
        """创建新工单"""
        case_id = self._generate_case_id()
        
        case = Case(
            case_id=case_id,
            client_id=case_create.client_id,
            title=case_create.title,
            description=case_create.description,
            status=CaseStatus.CREATED,
            trace_id=trace_id
        )
        
        created_case = await self.repository.create(case)
        
        logger.info(
            event="case_created",
            message=f"Created case {case_id}",
            case_id=case_id,
            client_id=case_create.client_id,
            trace_id=trace_id
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
        case_id: str,
        trace_id: Optional[str] = None
    ) -> Optional[CaseResponse]:
        """确认工单"""
        case = await self.repository.update_status(
            case_id, 
            CaseStatus.CONFIRMED,
            trace_id
        )
        if not case:
            return None
        
        logger.info(
            event="case_confirmed",
            message=f"Confirmed case {case_id}",
            case_id=case_id,
            trace_id=trace_id
        )
        
        return CaseResponse.model_validate(case)
    
    async def close_case(
        self, 
        case_id: str,
        trace_id: Optional[str] = None
    ) -> Optional[CaseResponse]:
        """关闭工单"""
        case = await self.repository.update_status(
            case_id, 
            CaseStatus.CLOSED,
            trace_id
        )
        if not case:
            return None
        
        logger.info(
            event="case_closed",
            message=f"Closed case {case_id}",
            case_id=case_id,
            trace_id=trace_id
        )
        
        return CaseResponse.model_validate(case)
