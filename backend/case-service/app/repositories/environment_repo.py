"""
Environment Repository - 环境数据访问层
"""

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.environment import Environment


class EnvironmentRepository:
    """环境数据访问层"""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(self, environment: Environment) -> Environment:
        """创建环境数据记录"""
        self.session.add(environment)
        await self.session.flush()
        await self.session.refresh(environment)
        return environment

    async def get_by_case_id(self, case_id: str) -> list[Environment]:
        """获取工单所有环境数据"""
        result = await self.session.execute(
            select(Environment)
            .where(Environment.case_id == case_id)
            .order_by(Environment.collected_at.desc().nullslast())
        )
        return list(result.scalars().all())

    async def get_by_case_and_type(self, case_id: str, env_type: str) -> Environment | None:
        """获取工单指定类型环境数据（最新一条）"""
        result = await self.session.execute(
            select(Environment)
            .where(Environment.case_id == case_id)
            .where(Environment.env_type == env_type)
            .order_by(Environment.collected_at.desc().nullslast())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def upsert_by_case_and_type(
        self,
        case_id: str,
        env_type: str,
        env_data: dict,
        collected_at=None,
    ) -> tuple["Environment", bool]:
        """
        upsert 环境数据（有则更新，无则创建）
        返回 (Environment, created: bool)
        """
        existing = await self.get_by_case_and_type(case_id, env_type)
        if existing:
            # 更新已有记录
            existing.env_data = env_data
            # 更新采集时间：显式传入则使用，否则默认当前时间（确保排序语义可靠）
            existing.collected_at = collected_at if collected_at is not None else datetime.now(timezone.utc)
            await self.session.flush()
            await self.session.refresh(existing)
            return existing, False
        else:
            # 创建新记录
            environment = Environment(
                case_id=case_id,
                env_type=env_type,
                env_data=env_data,
            )
            # 创建时：显式传入则使用，否则让 DB 默认值生效
            if collected_at is not None:
                environment.collected_at = collected_at
            self.session.add(environment)
            await self.session.flush()
            await self.session.refresh(environment)
            return environment, True

    async def delete_by_case_id(self, case_id: str) -> int:
        """删除工单所有环境数据（级联删除已由外键约束保证，此方法仅用于手动清理）"""
        result = await self.session.execute(
            select(Environment).where(Environment.case_id == case_id)
        )
        envs = list(result.scalars().all())
        for env in envs:
            await self.session.delete(env)
        await self.session.flush()
        return len(envs)
