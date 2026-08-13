"""采集计划数据访问层。"""

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.collection_plan import CollectionPlan, CollectionPlanItem


@dataclass(frozen=True, slots=True)
class CollectionPlanCreateResult:
    """采集计划幂等创建结果。"""

    entity: CollectionPlan
    items: list[CollectionPlanItem]
    created: bool


class CollectionPlanRepository:
    """采集计划仓储。"""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def create_with_items_idempotent(
        self,
        plan_values: dict[str, Any],
        item_values: list[dict[str, Any]],
    ) -> CollectionPlanCreateResult:
        """原子创建计划和计划项，重复幂等键返回原计划。"""

        statement = (
            insert(CollectionPlan)
            .values(**plan_values)
            .on_conflict_do_nothing(constraint="uq_collection_plan_tenant_idempotency")
            .returning(CollectionPlan.plan_id)
        )
        result = await self.session.execute(statement)
        created_plan_id = result.scalar_one_or_none()
        if created_plan_id is None:
            existing = await self.get_by_idempotency_key(
                plan_values["tenant_id"],
                plan_values["idempotency_key"],
            )
            if existing is None:
                raise RuntimeError("采集计划幂等冲突后无法读取原资源")
            return CollectionPlanCreateResult(
                entity=existing,
                items=await self.list_items(existing.plan_id),
                created=False,
            )

        entities = [CollectionPlanItem(plan_id=created_plan_id, **values) for values in item_values]
        self.session.add_all(entities)
        await self.session.flush()
        entity = await self.get_by_id_for_tenant(created_plan_id, plan_values["tenant_id"])
        if entity is None:
            raise RuntimeError("采集计划创建后无法读取")
        return CollectionPlanCreateResult(entity=entity, items=entities, created=True)

    async def get_by_idempotency_key(self, tenant_id: str, idempotency_key: str) -> CollectionPlan | None:
        """按租户和幂等键查询采集计划。"""

        result = await self.session.execute(
            select(CollectionPlan).where(
                CollectionPlan.tenant_id == tenant_id,
                CollectionPlan.idempotency_key == idempotency_key,
            )
        )
        return result.scalar_one_or_none()

    async def get_by_session_sequence(
        self,
        session_id: UUID | str,
        tenant_id: str,
        plan_sequence: int,
    ) -> CollectionPlan | None:
        """查询会话指定轮次的采集计划。"""

        result = await self.session.execute(
            select(CollectionPlan)
            .where(
                CollectionPlan.session_id == session_id,
                CollectionPlan.tenant_id == tenant_id,
                CollectionPlan.plan_sequence == plan_sequence,
                CollectionPlan.status == "ready",
            )
            .order_by(CollectionPlan.plan_revision.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def next_revision(self, session_id: UUID | str, plan_sequence: int) -> int:
        """计算同一会话和轮次的下一个计划修订号。"""

        result = await self.session.execute(
            select(func.coalesce(func.max(CollectionPlan.plan_revision), 0) + 1).where(
                CollectionPlan.session_id == session_id,
                CollectionPlan.plan_sequence == plan_sequence,
            )
        )
        return int(result.scalar_one())

    async def list_for_tenant(
        self,
        tenant_id: str,
        *,
        status: str | None = None,
        session_id: str | None = None,
        limit: int = 200,
    ) -> list[CollectionPlan]:
        """列出租户内采集计划，供管理端治理。"""

        statement = select(CollectionPlan).where(CollectionPlan.tenant_id == tenant_id)
        if status:
            statement = statement.where(CollectionPlan.status == status)
        if session_id:
            statement = statement.where(CollectionPlan.session_id == session_id)
        result = await self.session.execute(
            statement.order_by(CollectionPlan.created_at.desc(), CollectionPlan.plan_id).limit(limit)
        )
        return list(result.scalars().all())

    async def get_by_id_for_tenant(self, plan_id: UUID | str, tenant_id: str) -> CollectionPlan | None:
        """按租户读取采集计划。"""

        result = await self.session.execute(
            select(CollectionPlan).where(
                CollectionPlan.plan_id == plan_id,
                CollectionPlan.tenant_id == tenant_id,
            )
        )
        return result.scalar_one_or_none()

    async def get_by_id_for_update(self, plan_id: UUID | str, tenant_id: str) -> CollectionPlan | None:
        """锁定采集计划，用于串行化重生成。"""

        result = await self.session.execute(
            select(CollectionPlan)
            .where(CollectionPlan.plan_id == plan_id, CollectionPlan.tenant_id == tenant_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()

    async def list_items(self, plan_id: UUID | str) -> list[CollectionPlanItem]:
        """按稳定执行顺序读取采集计划项。"""

        result = await self.session.execute(
            select(CollectionPlanItem)
            .where(CollectionPlanItem.plan_id == plan_id)
            .order_by(CollectionPlanItem.sequence)
        )
        return list(result.scalars().all())
