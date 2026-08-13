"""Collection Profile（采集画像）生命周期服务。"""

import secrets
from contextlib import suppress
from datetime import UTC, datetime

from shared.dynamic_resource.loader import DynamicResourceLoader, ResourceNotFoundError
from shared.dynamic_resource.publisher import DynamicResourcePublisher
from shared.observability.otel import get_current_trace_id
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import ActorContext
from app.errors import DiagnosisError
from app.models.collection_profile_definition import CollectionProfileDefinitionEntity
from app.models.collector_definition import CollectorDefinition
from app.schemas.collection_profile import (
    CollectionProfileDefinition,
    CollectionProfilePublishRequest,
    CollectionProfileResponse,
    CollectionProfileReviewRequest,
    OfflineScenarioOptionResponse,
    profile_requires_affected_object,
)

COLLECTION_PROFILE_RESOURCE_TYPE = "collection_profile"


class CollectionProfileService:
    """维护画像草稿、审批和不可变运行时修订版本。"""

    def __init__(self, session: AsyncSession):
        self._session = session

    async def publish(
        self,
        *,
        actor: ActorContext,
        profile_id: str,
        command: CollectionProfilePublishRequest,
    ) -> CollectionProfileResponse:
        """兼容旧接口：保存草稿后立即审批发布。"""

        existing_result = await self._session.execute(
            select(CollectionProfileDefinitionEntity).where(CollectionProfileDefinitionEntity.profile_id == profile_id)
        )
        existing = existing_result.scalar_one_or_none()
        saved = await self.save_draft(
            actor=actor,
            profile_id=profile_id,
            command=command,
            if_match=str(existing.lock_version) if existing else None,
        )
        return await self.review(
            actor=actor,
            profile_id=profile_id,
            command=CollectionProfileReviewRequest(approved=True),
            if_match=str(saved.lock_version),
        )

    async def save_draft(
        self,
        *,
        actor: ActorContext,
        profile_id: str,
        command: CollectionProfilePublishRequest,
        if_match: str | None,
    ) -> CollectionProfileResponse:
        """创建或更新画像草稿；编辑已审批画像会重新进入草稿态。"""

        self._require_admin(actor)
        self._validate_identity(profile_id, command.profile)
        result = await self._session.execute(
            select(CollectionProfileDefinitionEntity)
            .where(CollectionProfileDefinitionEntity.profile_id == profile_id)
            .with_for_update()
        )
        entity = result.scalar_one_or_none()
        trace_id = get_current_trace_id() or secrets.token_hex(16)
        if entity is None:
            if if_match:
                raise DiagnosisError(code="COLLECTION_PROFILE_NOT_FOUND", message="采集画像不存在", http_status=404)
            entity = CollectionProfileDefinitionEntity(
                profile_id=profile_id,
                profile_json=command.profile.model_dump(mode="json"),
                managed_by="manual",
                generation_metadata={},
                semantic_version=command.version,
                review_status="draft",
                is_enabled=False,
                lock_version=1,
                trace_id=trace_id,
            )
            self._session.add(entity)
        else:
            self._reject_sync_managed(entity)
            self._assert_if_match(entity, if_match)
            entity.profile_json = command.profile.model_dump(mode="json")
            entity.semantic_version = command.version
            entity.review_status = "draft"
            entity.is_enabled = False
            entity.approved_by = None
            entity.approved_at = None
            entity.rejection_reason = None
            entity.lock_version += 1
            entity.trace_id = trace_id
        await self._session.flush()
        return await self._entity_response(entity)

    async def review(
        self,
        *,
        actor: ActorContext,
        profile_id: str,
        command: CollectionProfileReviewRequest,
        if_match: str | None,
    ) -> CollectionProfileResponse:
        """批准或拒绝画像；批准时校验 Collector 并发布不可变快照。"""

        self._require_admin(actor)
        entity = await self._get_locked(profile_id)
        self._reject_sync_managed(entity)
        self._assert_if_match(entity, if_match)
        entity.lock_version += 1
        entity.trace_id = get_current_trace_id() or entity.trace_id
        if not command.approved:
            entity.review_status = "rejected"
            entity.is_enabled = False
            entity.approved_by = None
            entity.approved_at = None
            entity.rejection_reason = (command.reason or "").strip()
            await self._session.flush()
            return await self._entity_response(entity)

        profile = CollectionProfileDefinition.model_validate(entity.profile_json)
        collector_dependencies = await self._validate_collectors(profile)
        snapshot = await DynamicResourcePublisher(self._session).ensure_published(
            resource_type=COLLECTION_PROFILE_RESOURCE_TYPE,
            resource_name=profile_id,
            version=entity.semantic_version,
            content=profile.model_dump(mode="json"),
            contract={
                "product_line": profile.product_line,
                "supported_product_versions": profile.supported_product_versions,
            },
            dependencies=collector_dependencies,
            trace_id=entity.trace_id,
        )
        entity.review_status = "approved"
        entity.is_enabled = True
        entity.approved_by = actor.user_id
        entity.approved_at = datetime.now(UTC)
        entity.rejection_reason = None
        await self._session.flush()
        return self._response(snapshot, profile, entity)

    async def disable(self, *, actor: ActorContext, profile_id: str, if_match: str | None) -> CollectionProfileResponse:
        """禁用画像，保留既有计划和动态修订用于审计。"""

        self._require_admin(actor)
        entity = await self._get_locked(profile_id)
        self._reject_sync_managed(entity)
        if entity.is_enabled:
            self._assert_if_match(entity, if_match)
            entity.is_enabled = False
            entity.lock_version += 1
            entity.trace_id = get_current_trace_id() or entity.trace_id
            await self._session.flush()
        return await self._entity_response(entity)

    async def get_active(self, *, actor: ActorContext, profile_id: str) -> CollectionProfileResponse:
        """读取当前生效画像。"""

        if not actor.has_any_role("platform_admin", "support_engineer", "diagnosis_worker"):
            raise DiagnosisError(code="FORBIDDEN", message="当前角色无权读取采集画像", http_status=403)
        entity = await self._get(profile_id)
        return await self._entity_response(entity)

    async def list_active(self, *, actor: ActorContext) -> list[CollectionProfileResponse]:
        """列出所有当前生效的不可变采集画像快照。"""

        if not actor.has_any_role("platform_admin", "support_engineer", "diagnosis_worker"):
            raise DiagnosisError(code="FORBIDDEN", message="当前角色无权读取采集画像", http_status=403)
        result = await self._session.execute(
            select(CollectionProfileDefinitionEntity).order_by(CollectionProfileDefinitionEntity.profile_id)
        )
        return [await self._entity_response(entity) for entity in result.scalars().all()]

    async def list_available_scenarios(self, *, actor: ActorContext) -> list[OfflineScenarioOptionResponse]:
        """列出客户可创建诊断会话的已审批、已启用且已发布画像。"""

        allowed_roles = {
            "customer_admin",
            "field_engineer",
            "support_engineer",
            "domain_expert",
            "platform_admin",
            "diagnosis_worker",
        }
        if not actor.has_any_role(*allowed_roles):
            raise DiagnosisError(code="FORBIDDEN", message="当前角色无权读取离线诊断场景", http_status=403)
        result = await self._session.execute(
            select(CollectionProfileDefinitionEntity)
            .where(
                CollectionProfileDefinitionEntity.review_status == "approved",
                CollectionProfileDefinitionEntity.is_enabled.is_(True),
            )
            .order_by(CollectionProfileDefinitionEntity.profile_id)
        )
        options: list[OfflineScenarioOptionResponse] = []
        for entity in result.scalars().all():
            try:
                snapshot = await self._load_snapshot(entity.profile_id)
            except DiagnosisError:
                continue
            profile = CollectionProfileDefinition.model_validate(snapshot.content)
            options.append(
                OfflineScenarioOptionResponse(
                    scenario=profile.scenario,
                    display_name=profile.display_name,
                    profile_revision=snapshot.revision,
                    profile_version=snapshot.version,
                    supported_product_versions=profile.supported_product_versions,
                    requires_affected_object=profile_requires_affected_object(profile),
                )
            )
        return options

    async def assert_scenario_available(self, scenario: str) -> CollectionProfileDefinition:
        """创建会话前校验场景对应画像仍处于可用状态。"""

        result = await self._session.execute(
            select(CollectionProfileDefinitionEntity).where(
                CollectionProfileDefinitionEntity.profile_id == scenario,
                CollectionProfileDefinitionEntity.review_status == "approved",
                CollectionProfileDefinitionEntity.is_enabled.is_(True),
            )
        )
        entity = result.scalar_one_or_none()
        if entity is None:
            raise DiagnosisError(
                code="COLLECTION_PROFILE_NOT_AVAILABLE",
                message="所选离线诊断场景尚未发布或已停用，请刷新后重新选择",
                http_status=409,
                details={"scenario": scenario},
            )
        try:
            snapshot = await self._load_snapshot(scenario)
        except DiagnosisError as exc:
            raise DiagnosisError(
                code="COLLECTION_PROFILE_NOT_AVAILABLE",
                message="所选离线诊断场景尚未发布或已停用，请刷新后重新选择",
                http_status=409,
                details={"scenario": scenario},
            ) from exc
        return CollectionProfileDefinition.model_validate(snapshot.content)

    async def _get(self, profile_id: str) -> CollectionProfileDefinitionEntity:
        result = await self._session.execute(
            select(CollectionProfileDefinitionEntity).where(CollectionProfileDefinitionEntity.profile_id == profile_id)
        )
        entity = result.scalar_one_or_none()
        if entity is None:
            raise DiagnosisError(code="COLLECTION_PROFILE_NOT_FOUND", message="采集画像不存在", http_status=404)
        return entity

    async def _get_locked(self, profile_id: str) -> CollectionProfileDefinitionEntity:
        result = await self._session.execute(
            select(CollectionProfileDefinitionEntity)
            .where(CollectionProfileDefinitionEntity.profile_id == profile_id)
            .with_for_update()
        )
        entity = result.scalar_one_or_none()
        if entity is None:
            raise DiagnosisError(code="COLLECTION_PROFILE_NOT_FOUND", message="采集画像不存在", http_status=404)
        return entity

    async def _entity_response(self, entity: CollectionProfileDefinitionEntity) -> CollectionProfileResponse:
        snapshot = None
        with suppress(DiagnosisError):
            snapshot = await self._load_snapshot(entity.profile_id)
        profile = CollectionProfileDefinition.model_validate(entity.profile_json)
        return self._response(snapshot, profile, entity)

    async def _validate_collectors(self, profile: CollectionProfileDefinition) -> list[dict]:
        collector_ids = sorted({item.collector_id for item in profile.items})
        result = await self._session.execute(
            select(CollectorDefinition).where(CollectorDefinition.collector_id.in_(collector_ids))
        )
        valid = {
            item.collector_id
            for item in result.scalars().all()
            if item.review_status == "approved" and item.is_enabled
        }
        missing = sorted(set(collector_ids) - valid)
        if missing:
            raise DiagnosisError(
                code="COLLECTION_PROFILE_COLLECTOR_UNAVAILABLE",
                message="采集画像引用了未审批、已禁用或不存在的 Collector",
                http_status=422,
                details={"collector_ids": missing},
            )
        loader = DynamicResourceLoader(self._session)
        dependencies = []
        for collector_id in collector_ids:
            try:
                snapshot = await loader.get_active("collector", collector_id)
            except ResourceNotFoundError as exc:
                raise DiagnosisError(
                    code="COLLECTION_PROFILE_COLLECTOR_REVISION_MISSING",
                    message="采集画像引用的 Collector 缺少生效修订",
                    http_status=422,
                    details={"collector_id": collector_id},
                ) from exc
            dependencies.append(
                {
                    "resource_type": "collector",
                    "resource_name": collector_id,
                    "revision": snapshot.revision,
                    "version": snapshot.version,
                    "checksum": snapshot.checksum,
                }
            )
        return dependencies

    async def _load_snapshot(self, profile_id: str):
        """加载画像快照并转换统一的不存在错误。"""

        try:
            return await DynamicResourceLoader(self._session).get_active(
                COLLECTION_PROFILE_RESOURCE_TYPE,
                profile_id,
            )
        except ResourceNotFoundError as exc:
            raise DiagnosisError(
                code="COLLECTION_PROFILE_NOT_FOUND",
                message="采集画像未发布或不存在",
                http_status=404,
                details={"profile_id": profile_id},
            ) from exc

    @staticmethod
    def _response(snapshot, profile: CollectionProfileDefinition, entity=None) -> CollectionProfileResponse:
        """生成稳定响应。"""

        return CollectionProfileResponse(
            profile=profile,
            revision=snapshot.revision if snapshot else None,
            version=entity.semantic_version if entity else snapshot.version,
            checksum=snapshot.checksum if snapshot else None,
            review_status=entity.review_status if entity else "approved",
            is_enabled=entity.is_enabled if entity else True,
            approved_by=entity.approved_by if entity else None,
            approved_at=entity.approved_at.isoformat() if entity and entity.approved_at else None,
            rejection_reason=entity.rejection_reason if entity else None,
            lock_version=entity.lock_version if entity else 1,
            managed_by=entity.managed_by if entity else "manual",
            generation_metadata=dict(entity.generation_metadata or {}) if entity else {},
            trace_id=entity.trace_id if entity else snapshot.trace_id,
            published_at=snapshot.published_at.isoformat() if snapshot and snapshot.published_at else None,
        )

    @staticmethod
    def _validate_identity(profile_id: str, profile: CollectionProfileDefinition) -> None:
        if profile.profile_id != profile_id:
            raise DiagnosisError(code="PROFILE_ID_MISMATCH", message="路径 profile_id 与请求体不一致", http_status=422)
        if profile.scenario != profile_id:
            raise DiagnosisError(
                code="PROFILE_SCENARIO_MISMATCH",
                message="profile_id 与 scenario 必须一一对应",
                http_status=422,
            )

    @staticmethod
    def _assert_if_match(entity: CollectionProfileDefinitionEntity, if_match: str | None) -> None:
        if if_match is None:
            raise DiagnosisError(code="IF_MATCH_REQUIRED", message="更新采集画像必须提供 If-Match", http_status=428)
        if if_match.strip().strip('"') != str(entity.lock_version):
            raise DiagnosisError(
                code="COLLECTION_PROFILE_VERSION_CONFLICT",
                message="采集画像已被其他请求更新",
                http_status=412,
                details={"current_version": entity.lock_version},
            )

    @staticmethod
    def _reject_sync_managed(entity: CollectionProfileDefinitionEntity) -> None:
        """KBD 同步画像只能由同步批次整体发布或回滚。"""

        if entity.managed_by == "kbd_sync":
            raise DiagnosisError(
                code="SYNC_MANAGED_PROFILE_READ_ONLY",
                message="该 Collection Profile 由“KBD 同步与版本”管理，请通过同步批次变更或回滚",
                http_status=409,
            )

    @staticmethod
    def _require_admin(actor: ActorContext) -> None:
        if not actor.has_any_role("platform_admin"):
            raise DiagnosisError(code="FORBIDDEN", message="当前角色无权管理采集画像", http_status=403)
