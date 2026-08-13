"""动态资源加载器。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.dynamic_resource import DynamicResourceActive, DynamicResourceRevision, DynamicResourceUsageAudit

from .cache import DynamicResourceCache
from .models import ResourceKey, ResourceSnapshot, UsageRecord
from .serialization import sha256_json


class ResourceNotFoundError(RuntimeError):
    """动态资源不存在或没有 active revision。"""


class DynamicResourceLoader:
    """统一动态资源运行时加载和审计入口。"""

    def __init__(self, session: AsyncSession, cache: DynamicResourceCache | None = None) -> None:
        self._session = session
        self._cache = cache

    async def get_active(self, resource_type: str, resource_name: str) -> ResourceSnapshot:
        """获取资源 active revision 快照。"""
        key = ResourceKey(resource_type=resource_type, resource_name=resource_name)
        if self._cache is not None:
            return await self._cache.get_or_load(key, lambda: self._load_active(key))
        return await self._load_active(key)

    async def get_revision(self, resource_type: str, resource_name: str, revision: int) -> ResourceSnapshot:
        """按运行时已加载的精确 revision 读取快照。

        使用审计不能在诊断结束时回读 active：期间若专家发布了新版本，active 已经
        指向另一份知识，会把旧执行结果错误归属给新 KBD。调用方从检索响应携带的
        ``resource_revision`` 传入该值，缺失时宁可放弃这条审计也不偷换版本。
        """

        result = await self._session.execute(
            select(DynamicResourceRevision).where(
                DynamicResourceRevision.resource_type == resource_type,
                DynamicResourceRevision.resource_name == resource_name,
                DynamicResourceRevision.revision == revision,
            )
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise ResourceNotFoundError(f"动态资源 revision 不存在: {resource_type}/{resource_name}@{revision}")
        return self._to_snapshot(row)

    async def list_active(self, resource_type: str) -> list[ResourceSnapshot]:
        """列出某类资源的所有 active 快照。"""
        result = await self._session.execute(
            select(DynamicResourceRevision)
            .join(
                DynamicResourceActive,
                (DynamicResourceActive.resource_type == DynamicResourceRevision.resource_type)
                & (DynamicResourceActive.resource_name == DynamicResourceRevision.resource_name)
                & (DynamicResourceActive.active_revision == DynamicResourceRevision.revision),
            )
            .where(DynamicResourceRevision.resource_type == resource_type)
            .order_by(DynamicResourceRevision.resource_name)
        )
        return [self._to_snapshot(row) for row in result.scalars().all()]

    async def audit_usage(self, snapshot: ResourceSnapshot, usage: UsageRecord) -> None:
        """写入资源使用审计。"""
        self._session.add(
            DynamicResourceUsageAudit(
                resource_type=snapshot.resource_type,
                resource_name=snapshot.resource_name,
                revision=snapshot.revision,
                consumer=usage.consumer,
                conversation_id=usage.conversation_id,
                case_id=usage.case_id,
                trace_id=usage.trace_id,
                exec_id=usage.exec_id,
                input_hash=sha256_json(usage.input_payload) if usage.input_payload is not None else None,
                output_hash=sha256_json(usage.output_payload) if usage.output_payload is not None else None,
                status=usage.status.value,
                error=usage.error,
                metadata_json=usage.metadata,
            )
        )
        await self._session.flush()

    async def _load_active(self, key: ResourceKey) -> ResourceSnapshot:
        result = await self._session.execute(
            select(DynamicResourceRevision)
            .join(
                DynamicResourceActive,
                (DynamicResourceActive.resource_type == DynamicResourceRevision.resource_type)
                & (DynamicResourceActive.resource_name == DynamicResourceRevision.resource_name)
                & (DynamicResourceActive.active_revision == DynamicResourceRevision.revision),
            )
            .where(
                DynamicResourceRevision.resource_type == key.resource_type,
                DynamicResourceRevision.resource_name == key.resource_name,
            )
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise ResourceNotFoundError(f"动态资源未发布或不存在: {key.resource_type}/{key.resource_name}")
        return self._to_snapshot(row)

    @staticmethod
    def _to_snapshot(row: DynamicResourceRevision) -> ResourceSnapshot:
        return ResourceSnapshot(
            resource_type=str(row.resource_type),
            resource_name=str(row.resource_name),
            revision=int(row.revision),
            version=str(row.version),
            status=str(row.status),
            content=dict(row.content_json or {}),
            contract=dict(row.contract_json or {}),
            dependencies=list(row.dependency_json or []),
            checksum=str(row.checksum),
            trace_id=row.trace_id,
            published_at=row.published_at,
        )


def snapshot_revision_metadata(snapshot: ResourceSnapshot) -> dict[str, Any]:
    """生成可写入业务结果的 revision 元数据。"""
    return {
        "resource_type": snapshot.resource_type,
        "resource_name": snapshot.resource_name,
        "revision": snapshot.revision,
        "version": snapshot.version,
        "checksum": snapshot.checksum,
    }
