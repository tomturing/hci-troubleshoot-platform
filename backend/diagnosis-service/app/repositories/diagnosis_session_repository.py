"""诊断会话数据访问层。"""

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.diagnosis_session import DiagnosisSession


@dataclass(frozen=True, slots=True)
class IdempotentCreateResult:
    """幂等创建结果。"""

    entity: DiagnosisSession
    created: bool


class DiagnosisSessionRepository:
    """诊断会话仓储。"""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def create_idempotent(self, values: dict[str, Any]) -> IdempotentCreateResult:
        """利用数据库唯一约束原子完成幂等创建。"""

        statement = (
            insert(DiagnosisSession)
            .values(**values)
            .on_conflict_do_nothing(constraint="uq_diagnosis_session_tenant_idempotency")
            .returning(DiagnosisSession.session_id)
        )
        result = await self.session.execute(statement)
        created_session_id = result.scalar_one_or_none()

        if created_session_id is not None:
            entity = await self.get_by_id_for_tenant(created_session_id, values["tenant_id"])
            if entity is None:
                raise RuntimeError("诊断会话创建后无法读取")
            return IdempotentCreateResult(entity=entity, created=True)

        existing = await self.get_by_idempotency_key(values["tenant_id"], values["idempotency_key"])
        if existing is None:
            raise RuntimeError("诊断会话幂等冲突后无法读取原资源")
        return IdempotentCreateResult(entity=existing, created=False)

    async def get_by_idempotency_key(self, tenant_id: str, idempotency_key: str) -> DiagnosisSession | None:
        """按租户和幂等键查询会话。"""

        result = await self.session.execute(
            select(DiagnosisSession).where(
                DiagnosisSession.tenant_id == tenant_id,
                DiagnosisSession.idempotency_key == idempotency_key,
            )
        )
        return result.scalar_one_or_none()

    async def get_by_id_for_tenant(self, session_id: UUID | str, tenant_id: str) -> DiagnosisSession | None:
        """按租户读取会话，避免泄露其他租户对象是否存在。"""

        result = await self.session.execute(
            select(DiagnosisSession).where(
                DiagnosisSession.session_id == session_id,
                DiagnosisSession.tenant_id == tenant_id,
            )
        )
        return result.scalar_one_or_none()

    async def get_latest_by_case(self, case_id: str, tenant_id: str) -> DiagnosisSession | None:
        """按工单读取最近一个未删除的离线诊断会话。"""

        result = await self.session.execute(
            select(DiagnosisSession)
            .where(
                DiagnosisSession.case_id == case_id,
                DiagnosisSession.tenant_id == tenant_id,
                DiagnosisSession.status != "deleted",
            )
            .order_by(DiagnosisSession.created_at.desc(), DiagnosisSession.session_id.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def get_by_id_for_update(self, session_id: UUID | str, tenant_id: str) -> DiagnosisSession | None:
        """锁定会话后读取，保证状态转换串行执行。"""

        result = await self.session.execute(
            select(DiagnosisSession)
            .where(
                DiagnosisSession.session_id == session_id,
                DiagnosisSession.tenant_id == tenant_id,
            )
            .with_for_update()
        )
        return result.scalar_one_or_none()

    async def flush(self, entity: DiagnosisSession) -> DiagnosisSession:
        """刷新实体变更。"""

        await self.session.flush()
        await self.session.refresh(entity)
        return entity
