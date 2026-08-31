"""动态资源发布器。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.dynamic_resource import DynamicResourceActive, DynamicResourceRevision

from .loader import DynamicResourceLoader
from .models import ResourceSnapshot
from .serialization import resource_checksum


class DynamicResourcePublisher:
    """把业务表记录同步为动态资源 revision。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def ensure_published(
        self,
        *,
        resource_type: str,
        resource_name: str,
        version: str,
        content: dict[str, Any],
        contract: dict[str, Any] | None = None,
        dependencies: list[dict[str, Any]] | None = None,
        status: str = "published",
        trace_id: str | None = None,
        package_snapshot_digest: str | None = None,
        knowledge_snapshot_digest: str | None = None,
        release_id: UUID | None = None,
    ) -> ResourceSnapshot:
        """
        确保资源内容存在对应 revision，并把 active 指针切过去。

        同一 resource_type/resource_name/checksum 已存在时复用旧 revision，
        避免读路径或重复保存制造无意义版本。
        """
        contract_json = contract or {}
        dependency_json = dependencies or []
        await self._session.execute(
            select(func.pg_advisory_xact_lock(func.hashtextextended(f"{resource_type}:{resource_name}", 0)))
        )
        checksum = resource_checksum(
            content,
            contract_json,
            dependency_json,
            version=version,
            status=status,
        )

        existing_result = await self._session.execute(
            select(DynamicResourceRevision).where(
                DynamicResourceRevision.resource_type == resource_type,
                DynamicResourceRevision.resource_name == resource_name,
                DynamicResourceRevision.checksum == checksum,
            )
        )
        revision_row = existing_result.scalar_one_or_none()
        created = revision_row is None
        if revision_row is None:
            next_revision_result = await self._session.execute(
                select(func.coalesce(func.max(DynamicResourceRevision.revision), 0) + 1).where(
                    DynamicResourceRevision.resource_type == resource_type,
                    DynamicResourceRevision.resource_name == resource_name,
                )
            )
            next_revision = int(next_revision_result.scalar_one())
            revision_row = DynamicResourceRevision(
                resource_type=resource_type,
                resource_name=resource_name,
                version=version,
                revision=next_revision,
                status=status,
                content_json=content,
                contract_json=contract_json,
                dependency_json=dependency_json,
                checksum=checksum,
                trace_id=trace_id,
                published_at=datetime.now(UTC) if status == "published" else None,
                package_snapshot_digest=package_snapshot_digest,
                knowledge_snapshot_digest=knowledge_snapshot_digest,
                release_id=release_id,
            )
            self._session.add(revision_row)
            await self._session.flush()
        elif any(
            expected is not None and actual is not None and str(actual) != str(expected)
            for actual, expected in (
                (revision_row.package_snapshot_digest, package_snapshot_digest),
                (revision_row.knowledge_snapshot_digest, knowledge_snapshot_digest),
                (revision_row.release_id, release_id),
            )
        ):
            raise RuntimeError("immutable dynamic resource revision identity conflict")

        active = await self._session.get(
            DynamicResourceActive,
            {
                "resource_type": resource_type,
                "resource_name": resource_name,
            },
        )
        if active is None:
            self._session.add(
                DynamicResourceActive(
                    resource_type=resource_type,
                    resource_name=resource_name,
                    active_revision=revision_row.revision,
                    checksum=checksum,
                    trace_id=trace_id,
                    generation=1,
                    desired_revision=revision_row.revision,
                    desired_checksum=checksum,
                )
            )
        else:
            if active.active_revision != revision_row.revision or active.checksum != checksum:
                active.active_revision = revision_row.revision
                active.checksum = checksum
                active.generation += 1
                active.desired_revision = revision_row.revision
                active.desired_checksum = checksum
            # generation 只描述指针目标变化；trace_id/updated_at 记录最后一次幂等发布调用。
            active.trace_id = trace_id
            active.updated_at = datetime.now(UTC)

        if created:
            revision_row.package_snapshot_digest = package_snapshot_digest
            revision_row.knowledge_snapshot_digest = knowledge_snapshot_digest
            revision_row.release_id = release_id

        await self._session.flush()
        return DynamicResourceLoader._to_snapshot(revision_row)
