"""Collector Artifact 数据访问层。"""

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.collector_artifact import CollectorArtifact, CollectorArtifactItem


@dataclass(frozen=True, slots=True)
class CollectorArtifactCreateResult:
    """制品幂等创建结果。"""

    entity: CollectorArtifact
    items: list[CollectorArtifactItem]
    created: bool


class CollectorArtifactRepository:
    """Collector Artifact 仓储。"""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def create_with_items_idempotent(
        self,
        artifact_values: dict[str, Any],
        item_values: list[dict[str, Any]],
    ) -> CollectorArtifactCreateResult:
        """原子创建制品及其 Collector 快照项。"""

        statement = (
            insert(CollectorArtifact)
            .values(**artifact_values)
            .on_conflict_do_nothing(constraint="uq_collector_artifact_tenant_idempotency")
            .returning(CollectorArtifact.artifact_id)
        )
        result = await self.session.execute(statement)
        artifact_id = result.scalar_one_or_none()
        if artifact_id is None:
            existing = await self.get_by_idempotency_key(
                artifact_values["tenant_id"],
                artifact_values["idempotency_key"],
            )
            if existing is None:
                raise RuntimeError("Collector Artifact 幂等冲突后无法读取原资源")
            return CollectorArtifactCreateResult(
                entity=existing,
                items=await self.list_items(existing.artifact_id),
                created=False,
            )

        items = [CollectorArtifactItem(artifact_id=artifact_id, **values) for values in item_values]
        self.session.add_all(items)
        await self.session.flush()
        entity = await self.get_by_id_for_tenant(artifact_id, artifact_values["tenant_id"])
        if entity is None:
            raise RuntimeError("Collector Artifact 创建后无法读取")
        return CollectorArtifactCreateResult(entity=entity, items=items, created=True)

    async def get_by_idempotency_key(self, tenant_id: str, idempotency_key: str) -> CollectorArtifact | None:
        """按租户和幂等键查询制品。"""

        result = await self.session.execute(
            select(CollectorArtifact).where(
                CollectorArtifact.tenant_id == tenant_id,
                CollectorArtifact.idempotency_key == idempotency_key,
            )
        )
        return result.scalar_one_or_none()

    async def get_by_plan_target(
        self,
        collection_plan_id: UUID | str,
        tenant_id: str,
        target_key: str,
    ) -> CollectorArtifact | None:
        """按计划和目标查询唯一制品。"""

        result = await self.session.execute(
            select(CollectorArtifact).where(
                CollectorArtifact.collection_plan_id == collection_plan_id,
                CollectorArtifact.tenant_id == tenant_id,
                CollectorArtifact.target_key == target_key,
            )
        )
        return result.scalar_one_or_none()

    async def get_by_id_for_tenant(self, artifact_id: UUID | str, tenant_id: str) -> CollectorArtifact | None:
        """按租户查询制品。"""

        result = await self.session.execute(
            select(CollectorArtifact).where(
                CollectorArtifact.artifact_id == artifact_id,
                CollectorArtifact.tenant_id == tenant_id,
            )
        )
        return result.scalar_one_or_none()

    async def get_by_id_for_update(self, artifact_id: UUID | str, tenant_id: str) -> CollectorArtifact | None:
        """锁定制品后读取，用于撤销状态转换。"""

        result = await self.session.execute(
            select(CollectorArtifact)
            .where(
                CollectorArtifact.artifact_id == artifact_id,
                CollectorArtifact.tenant_id == tenant_id,
            )
            .with_for_update()
        )
        return result.scalar_one_or_none()

    async def list_items(self, artifact_id: UUID | str) -> list[CollectorArtifactItem]:
        """按稳定顺序查询制品项。"""

        result = await self.session.execute(
            select(CollectorArtifactItem)
            .where(CollectorArtifactItem.artifact_id == artifact_id)
            .order_by(CollectorArtifactItem.sequence)
        )
        return list(result.scalars().all())

    async def list_revoked_for_tenant(self, tenant_id: str) -> list[CollectorArtifact]:
        """按撤销时间稳定查询租户范围内的已撤销制品。"""

        result = await self.session.execute(
            select(CollectorArtifact)
            .where(
                CollectorArtifact.tenant_id == tenant_id,
                CollectorArtifact.status == "revoked",
            )
            .order_by(CollectorArtifact.revoked_at, CollectorArtifact.artifact_id)
        )
        return list(result.scalars().all())

    async def list_for_tenant(
        self,
        tenant_id: str,
        *,
        status: str | None = None,
        session_id: str | None = None,
        limit: int = 200,
    ) -> list[CollectorArtifact]:
        """列出租户内制品，供管理端执行撤销和审计。"""

        statement = select(CollectorArtifact).where(CollectorArtifact.tenant_id == tenant_id)
        if status:
            statement = statement.where(CollectorArtifact.status == status)
        if session_id:
            statement = statement.where(CollectorArtifact.session_id == session_id)
        result = await self.session.execute(
            statement.order_by(CollectorArtifact.created_at.desc(), CollectorArtifact.artifact_id).limit(limit)
        )
        return list(result.scalars().all())
