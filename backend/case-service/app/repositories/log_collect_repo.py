"""日志收集任务数据访问层。"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.log_collect import LogCollectTask
from ..schemas.log_collect import ACTIVE_STATUSES


class LogCollectTaskRepository:
    """任务表 CRUD。

    事务边界由 DatabaseManager 的 session 生成器统一提交，本层只 flush。
    """

    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(self, task: LogCollectTask) -> LogCollectTask:
        self.session.add(task)
        await self.session.flush()
        await self.session.refresh(task)
        return task

    async def get_by_id(self, task_id) -> LogCollectTask | None:
        result = await self.session.execute(select(LogCollectTask).where(LogCollectTask.task_id == task_id))
        return result.scalar_one_or_none()

    async def get_active_by_case(self, case_id: str) -> LogCollectTask | None:
        """返回该工单的活跃任务（同一工单最多一个，见部分唯一索引）。"""
        result = await self.session.execute(
            select(LogCollectTask)
            .where(LogCollectTask.case_id == case_id)
            .where(LogCollectTask.status.in_(ACTIVE_STATUSES))
            .order_by(LogCollectTask.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def list_by_case(self, case_id: str, *, limit: int = 20, offset: int = 0) -> tuple[list[LogCollectTask], int]:
        total_result = await self.session.execute(
            select(func.count()).select_from(LogCollectTask).where(LogCollectTask.case_id == case_id)
        )
        total = int(total_result.scalar_one() or 0)
        result = await self.session.execute(
            select(LogCollectTask)
            .where(LogCollectTask.case_id == case_id)
            .order_by(LogCollectTask.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(result.scalars().all()), total

    async def flush(self) -> None:
        await self.session.flush()

    async def refresh(self, task: LogCollectTask) -> None:
        await self.session.refresh(task)
