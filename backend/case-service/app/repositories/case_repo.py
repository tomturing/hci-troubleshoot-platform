"""
Case Repository
"""

from typing import List, Optional
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime

from ..models.case import Case, CaseStatus
from shared.models.user import User

class CaseRepository:
    """工单数据访问层"""
    
    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_user_by_client_id(self, client_id: str) -> Optional[User]:
        """根据client_id查询用户"""
        result = await self.session.execute(
            select(User).where(User.client_id == client_id)
        )
        return result.scalar_one_or_none()

    async def create_user(self, user: User) -> User:
        """创建用户"""
        self.session.add(user)
        await self.session.flush()
        await self.session.refresh(user)
        return user
    
    async def create(self, case: Case) -> Case:
        """创建工单"""
        self.session.add(case)
        await self.session.flush()
        await self.session.refresh(case)
        return case
    
    async def get_by_id(self, case_id: str) -> Optional[Case]:
        """根据case_id查询工单"""
        result = await self.session.execute(
            select(Case).where(Case.case_id == case_id)
        )
        return result.scalar_one_or_none()
    
    async def get_by_client_id(self, client_id: str) -> List[Case]:
        """根据client_id查询工单列表"""
        result = await self.session.execute(
            select(Case)
            .where(Case.client_id == client_id)
            .order_by(Case.created_at.desc())
        )
        return list(result.scalars().all())
    
    async def update_status(
        self, 
        case_id: str, 
        status: CaseStatus,
        trace_id: Optional[str] = None
    ) -> Optional[Case]:
        """更新工单状态"""
        case = await self.get_by_id(case_id)
        if not case:
            return None
        
        case.status = status
        if status == CaseStatus.closed:
            case.closed_at = datetime.utcnow()
        if trace_id:
            case.trace_id = trace_id
        
        await self.session.flush()
        await self.session.refresh(case)
        return case
    
    async def delete(self, case_id: str) -> bool:
        """删除工单"""
        case = await self.get_by_id(case_id)
        if not case:
            return False
        
        await self.session.delete(case)
        await self.session.flush()
        return True
