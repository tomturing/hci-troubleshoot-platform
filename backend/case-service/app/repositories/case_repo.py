"""
Case Repository
"""

from datetime import UTC, datetime

from shared.models.user import User
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.case import Case, CaseStatus


class CaseRepository:
    """工单数据访问层"""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_user_by_client_id(self, client_id: str) -> User | None:
        """根据client_id查询用户"""
        result = await self.session.execute(select(User).where(User.client_id == client_id))
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

    async def generate_case_id(self) -> str:
        """通过数据库函数生成工单号，保证同日递增与并发安全"""
        result = await self.session.execute(text("SELECT generate_case_id()"))
        case_id = result.scalar_one_or_none()
        if not case_id:
            raise RuntimeError("数据库未返回有效工单号")
        return str(case_id)

    async def get_by_id(self, case_id: str) -> Case | None:
        """根据case_id查询工单"""
        result = await self.session.execute(select(Case).where(Case.case_id == case_id))
        return result.scalar_one_or_none()

    async def get_by_client_id(self, client_id: str) -> list[Case]:
        """根据client_id查询工单列表"""
        result = await self.session.execute(
            select(Case).where(Case.client_id == client_id).order_by(Case.created_at.desc())
        )
        return list(result.scalars().all())

    async def update_status(
        self,
        case_id: str,
        status: CaseStatus,
        close_reason: str | None = None,
    ) -> Case | None:
        """更新工单状态

        Args:
            case_id: 工单ID
            status: 目标状态
            close_reason: 关闭原因（可选），用于 S0 失败/S6 选 C 等场景

        Returns:
            更新后的 Case 对象，或 None（工单不存在）
        """
        case = await self.get_by_id(case_id)
        if not case:
            return None

        case.status = status
        if close_reason:
            case.close_reason = close_reason
        if status == CaseStatus.closed:
            case.closed_at = datetime.now(UTC)

        await self.session.flush()
        await self.session.refresh(case)
        return case

    async def update_close_reason(self, case_id: str, close_reason: str) -> Case | None:
        """更新工单关闭原因"""
        case = await self.get_by_id(case_id)
        if not case:
            return None

        case.close_reason = close_reason
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

    async def get_all(
        self,
        skip: int = 0,
        limit: int = 20,
        status: str | None = None,
        client_id: str | None = None,
        start_time: datetime | None = None,
        end_time: datetime | None = None,
    ) -> tuple[list[Case], int]:
        """获取所有工单（分页 + 筛选），返回 (items, total)"""
        query = select(Case)
        count_query = select(func.count()).select_from(Case)

        # 构建筛选条件
        if status:
            query = query.where(Case.status == status)
            count_query = count_query.where(Case.status == status)
        if client_id:
            query = query.where(Case.client_id == client_id)
            count_query = count_query.where(Case.client_id == client_id)
        if start_time:
            query = query.where(Case.created_at >= start_time)
            count_query = count_query.where(Case.created_at >= start_time)
        if end_time:
            query = query.where(Case.created_at <= end_time)
            count_query = count_query.where(Case.created_at <= end_time)

        # 总数
        total_result = await self.session.execute(count_query)
        total = total_result.scalar() or 0

        # 分页数据
        query = query.order_by(Case.created_at.desc()).offset(skip).limit(limit)
        result = await self.session.execute(query)
        items = list(result.scalars().all())

        return items, total

    async def count_by_status(self) -> dict[str, int]:
        """按状态统计工单数量"""
        query = select(Case.status, func.count()).group_by(Case.status)
        result = await self.session.execute(query)
        return {str(row[0].value): row[1] for row in result.all()}

    async def get_client_stats(self) -> list[dict]:
        """获取客户端列表及其工单数"""
        query = (
            select(
                Case.client_id,
                func.count().label("case_count"),
                func.max(Case.created_at).label("last_case_at"),
            )
            .group_by(Case.client_id)
            .order_by(func.count().desc())
        )
        result = await self.session.execute(query)
        return [
            {
                "client_id": row[0],
                "case_count": row[1],
                "last_case_at": row[2],
            }
            for row in result.all()
        ]
