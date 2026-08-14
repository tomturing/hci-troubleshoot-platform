"""安全 Collector Registry（采集器注册表）服务。"""

import secrets
from datetime import UTC, datetime

from shared.dynamic_resource.loader import DynamicResourceLoader, ResourceNotFoundError
from shared.dynamic_resource.publisher import DynamicResourcePublisher
from shared.observability.otel import get_current_trace_id
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import ActorContext
from app.domain.collector_security import (
    validate_collector_contract,
    validate_hci_api_contract,
    validate_manual_guide,
)
from app.errors import DiagnosisError
from app.models.collector_definition import CollectorDefinition
from app.schemas.collector_definition import (
    CollectorApprovalRequest,
    CollectorDefinitionResponse,
    CollectorDefinitionWrite,
)

COLLECTOR_RESOURCE_TYPE = "collector"


class CollectorDefinitionService:
    """维护 Collector 草稿、审批、禁用和运行时修订版本。"""

    def __init__(self, session: AsyncSession):
        self._session = session

    async def save_draft(
        self,
        *,
        actor: ActorContext,
        collector_id: str,
        command: CollectorDefinitionWrite,
        if_match: str | None,
    ) -> CollectorDefinitionResponse:
        """创建或更新 Collector 草稿，更新已审批记录会重新进入草稿态。"""

        self._require_admin(actor)
        if command.collector_id != collector_id:
            raise DiagnosisError(
                code="COLLECTOR_ID_MISMATCH",
                message="路径 collector_id 与请求体不一致",
                http_status=422,
            )
        self._validate_definition(command)
        result = await self._session.execute(
            select(CollectorDefinition).where(CollectorDefinition.collector_id == collector_id).with_for_update()
        )
        entity = result.scalar_one_or_none()
        trace_id = get_current_trace_id() or secrets.token_hex(16)
        if entity is None:
            if collector_id.startswith("kbd_"):
                raise DiagnosisError(
                    code="SYNC_MANAGED_COLLECTOR_RESERVED",
                    message="kbd_ 命名空间由“KBD 同步与版本”生成，不能手工创建",
                    http_status=409,
                )
            if if_match:
                raise DiagnosisError(
                    code="COLLECTOR_NOT_FOUND",
                    message="Collector 不存在，创建时不能提供 If-Match",
                    http_status=404,
                )
            entity = CollectorDefinition(
                collector_id=collector_id,
                review_status="draft",
                is_enabled=False,
                lock_version=1,
                trace_id=trace_id,
            )
            self._session.add(entity)
        else:
            self._reject_sync_managed(entity)
            self._assert_if_match(entity, if_match)
            entity.lock_version += 1
            entity.review_status = "draft"
            entity.is_enabled = False
            entity.approved_by = None
            entity.approved_at = None
            entity.rejection_reason = None
            entity.trace_id = trace_id

        self._apply_definition(entity, command)
        await self._session.flush()
        return CollectorDefinitionResponse.from_entity(entity)

    async def approve(
        self,
        *,
        actor: ActorContext,
        collector_id: str,
        command: CollectorApprovalRequest,
        if_match: str | None,
    ) -> CollectorDefinitionResponse:
        """审批或拒绝 Collector；只有批准时发布动态资源版本。"""

        self._require_admin(actor)
        entity = await self._get_locked(collector_id)
        self._reject_sync_managed(entity)
        if entity.review_status == "approved" and command.approved:
            snapshot = await self._active_snapshot(collector_id)
            return CollectorDefinitionResponse.from_entity(entity, snapshot=snapshot)
        if entity.review_status == "rejected" and not command.approved and entity.rejection_reason == command.reason:
            return CollectorDefinitionResponse.from_entity(entity)
        self._assert_if_match(entity, if_match)

        entity.lock_version += 1
        entity.trace_id = get_current_trace_id() or entity.trace_id
        if not command.approved:
            entity.review_status = "rejected"
            entity.is_enabled = False
            entity.approved_by = None
            entity.approved_at = None
            entity.rejection_reason = command.reason
            await self._session.flush()
            return CollectorDefinitionResponse.from_entity(entity)

        self._validate_definition(CollectorDefinitionWrite.model_validate(self._resource_content(entity)))
        entity.review_status = "approved"
        entity.is_enabled = True
        entity.approved_by = actor.user_id
        entity.approved_at = datetime.now(UTC)
        entity.rejection_reason = None
        snapshot = await DynamicResourcePublisher(self._session).ensure_published(
            resource_type=COLLECTOR_RESOURCE_TYPE,
            resource_name=collector_id,
            version=entity.semantic_version,
            content=self._resource_content(entity),
            contract={
                "parameter_schema": entity.parameter_schema,
                "output_contract": entity.output_contract,
            },
            trace_id=entity.trace_id,
        )
        await self._session.flush()
        return CollectorDefinitionResponse.from_entity(entity, snapshot=snapshot)

    async def disable(
        self,
        *,
        actor: ActorContext,
        collector_id: str,
        if_match: str | None,
    ) -> CollectorDefinitionResponse:
        """禁用 Collector；保留历史动态资源快照用于审计。"""

        self._require_admin(actor)
        entity = await self._get_locked(collector_id)
        self._reject_sync_managed(entity)
        if not entity.is_enabled:
            return CollectorDefinitionResponse.from_entity(entity)
        self._assert_if_match(entity, if_match)
        entity.is_enabled = False
        entity.lock_version += 1
        entity.trace_id = get_current_trace_id() or entity.trace_id
        await self._invalidate_consumers(collector_id, actor.user_id, entity.trace_id)
        await self._session.flush()
        return CollectorDefinitionResponse.from_entity(entity)

    async def _invalidate_consumers(self, collector_id: str, actor_id: str, trace_id: str) -> None:
        """禁用 Collector 时作废引用计划并撤销仍可下载的制品。"""

        now = datetime.now(UTC)
        await self._session.execute(
            text(
                """
                UPDATE collection_plan p
                SET status = 'superseded', updated_at = CURRENT_TIMESTAMP
                WHERE p.status = 'ready'
                  AND EXISTS (
                      SELECT 1 FROM collection_plan_item i
                      WHERE i.plan_id = p.plan_id AND i.collector_id = :collector_id
                  )
                """
            ),
            {"collector_id": collector_id},
        )
        await self._session.execute(
            text(
                """
                UPDATE collector_artifact a
                SET status = 'revoked', revoked_at = :revoked_at, revoked_by = :actor_id,
                    revocation_reason = :reason, revoked_trace_id = :trace_id,
                    updated_at = CURRENT_TIMESTAMP
                WHERE a.status = 'ready'
                  AND EXISTS (
                      SELECT 1 FROM collector_artifact_item i
                      WHERE i.artifact_id = a.artifact_id AND i.collector_id = :collector_id
                  )
                """
            ),
            {
                "collector_id": collector_id,
                "revoked_at": now,
                "actor_id": actor_id,
                "reason": "collector_disabled",
                "trace_id": trace_id,
            },
        )

    async def get(self, *, actor: ActorContext, collector_id: str) -> CollectorDefinitionResponse:
        """读取 Collector 当前定义和生效修订版本。"""

        self._require_admin(actor)
        entity = await self._get(collector_id)
        snapshot = await self._active_snapshot(collector_id, required=False)
        return CollectorDefinitionResponse.from_entity(entity, snapshot=snapshot)

    async def list(
        self,
        *,
        actor: ActorContext,
        review_status: str | None = None,
        is_enabled: bool | None = None,
    ) -> list[CollectorDefinitionResponse]:
        """按稳定顺序列出 Collector，并附带各自当前生效修订版本。"""

        self._require_admin(actor)
        statement = select(CollectorDefinition)
        if review_status is not None:
            statement = statement.where(CollectorDefinition.review_status == review_status)
        if is_enabled is not None:
            statement = statement.where(CollectorDefinition.is_enabled == is_enabled)
        result = await self._session.execute(statement.order_by(CollectorDefinition.collector_id))
        responses = []
        for entity in result.scalars().all():
            snapshot = await self._active_snapshot(entity.collector_id, required=False)
            responses.append(CollectorDefinitionResponse.from_entity(entity, snapshot=snapshot))
        return responses

    async def _get(self, collector_id: str) -> CollectorDefinition:
        result = await self._session.execute(
            select(CollectorDefinition).where(CollectorDefinition.collector_id == collector_id)
        )
        entity = result.scalar_one_or_none()
        if entity is None:
            raise DiagnosisError(code="COLLECTOR_NOT_FOUND", message="Collector 不存在", http_status=404)
        return entity

    async def _get_locked(self, collector_id: str) -> CollectorDefinition:
        result = await self._session.execute(
            select(CollectorDefinition).where(CollectorDefinition.collector_id == collector_id).with_for_update()
        )
        entity = result.scalar_one_or_none()
        if entity is None:
            raise DiagnosisError(code="COLLECTOR_NOT_FOUND", message="Collector 不存在", http_status=404)
        return entity

    async def _active_snapshot(self, collector_id: str, *, required: bool = True):
        try:
            return await DynamicResourceLoader(self._session).get_active(COLLECTOR_RESOURCE_TYPE, collector_id)
        except ResourceNotFoundError as exc:
            if not required:
                return None
            raise DiagnosisError(
                code="COLLECTOR_REVISION_NOT_FOUND",
                message="Collector 已审批但运行时修订版本不存在",
                http_status=409,
            ) from exc

    @staticmethod
    def _apply_definition(entity: CollectorDefinition, command: CollectorDefinitionWrite) -> None:
        entity.display_name = command.display_name
        entity.description = command.description
        entity.platform = command.platform
        entity.executor = command.executor
        entity.command_template = command.command_template
        entity.parameter_schema = command.parameter_schema
        entity.risk_level = command.risk_level
        entity.timeout_seconds = command.timeout_seconds
        entity.max_output_mb = command.max_output_mb
        entity.supported_product_versions = command.supported_product_versions
        entity.output_contract = command.output_contract.model_dump(mode="json")
        entity.managed_by = "manual"
        entity.generation_metadata = {}
        entity.semantic_version = command.version

    @staticmethod
    def _resource_content(entity: CollectorDefinition) -> dict:
        return {
            "collector_id": entity.collector_id,
            "display_name": entity.display_name,
            "description": entity.description,
            "platform": entity.platform,
            "executor": entity.executor,
            "command_template": entity.command_template,
            "parameter_schema": entity.parameter_schema,
            "risk_level": entity.risk_level,
            "timeout_seconds": entity.timeout_seconds,
            "max_output_mb": float(entity.max_output_mb),
            "supported_product_versions": entity.supported_product_versions,
            "output_contract": entity.output_contract,
            "version": entity.semantic_version,
            "managed_by": entity.managed_by,
            "generation_metadata": entity.generation_metadata,
        }

    @staticmethod
    def _reject_sync_managed(entity: CollectorDefinition) -> None:
        """同步生成资源只能通过同步批次发布或回滚，禁止旁路修改。"""

        if entity.managed_by == "kbd_sync" or entity.collector_id.startswith("kbd_"):
            raise DiagnosisError(
                code="SYNC_MANAGED_COLLECTOR_READ_ONLY",
                message="该 Collector 由“KBD 同步与版本”管理，请通过同步批次变更或回滚",
                http_status=409,
            )

    @staticmethod
    def _validate_definition(command: CollectorDefinitionWrite) -> None:
        """按执行器类型执行默认拒绝的安全契约校验。"""

        if command.executor == "shell":
            validate_collector_contract(command.command_template, command.parameter_schema)
        elif command.executor == "http":
            validate_hci_api_contract(command.command_template, command.parameter_schema)
        else:
            validate_manual_guide(command.command_template, command.parameter_schema)

    @staticmethod
    def _assert_if_match(entity: CollectorDefinition, if_match: str | None) -> None:
        if if_match is None:
            raise DiagnosisError(
                code="IF_MATCH_REQUIRED",
                message="更新 Collector 必须提供 If-Match",
                http_status=428,
            )
        if if_match.strip().strip('"') != str(entity.lock_version):
            raise DiagnosisError(
                code="COLLECTOR_VERSION_CONFLICT",
                message="Collector 已被其他请求更新",
                http_status=412,
                details={"current_version": entity.lock_version},
            )

    @staticmethod
    def _require_admin(actor: ActorContext) -> None:
        if not actor.has_any_role("platform_admin"):
            raise DiagnosisError(code="FORBIDDEN", message="当前角色无权管理 Collector", http_status=403)
