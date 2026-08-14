"""Collection Plan（采集计划）生成服务。"""

import hashlib
import json
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import ValidationError
from shared.dynamic_resource.loader import DynamicResourceLoader, ResourceNotFoundError
from shared.dynamic_resource.models import UsageRecord, UsageStatus
from shared.observability.logger import get_logger
from shared.observability.otel import get_current_trace_id
from shared.resolution.product_versions import matches_any_product_version
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import ActorContext
from app.domain.session_state import DiagnosisSessionStatus
from app.errors import DiagnosisError
from app.repositories.collection_plan_repository import CollectionPlanCreateResult, CollectionPlanRepository
from app.repositories.diagnosis_session_repository import DiagnosisSessionRepository
from app.schemas.collection_plan import CollectionPlanGenerateRequest, CollectionPlanRegenerateRequest
from app.schemas.collection_profile import (
    CollectionCondition,
    CollectionProfileDefinition,
    CollectionProfileItem,
    CollectionRequirementLevel,
)
from app.schemas.collector_definition import CollectorDefinitionWrite
from app.services.collection_profile_service import COLLECTION_PROFILE_RESOURCE_TYPE

logger = get_logger("collection-plan-service")
PLAN_ROLES = frozenset({"platform_admin", "support_engineer", "diagnosis_worker"})


class CollectionPlanService:
    """把生效画像和会话上下文确定性展开为采集计划。"""

    def __init__(
        self,
        *,
        session: AsyncSession,
        session_repository: DiagnosisSessionRepository,
        plan_repository: CollectionPlanRepository,
    ):
        self._session = session
        self._session_repository = session_repository
        self._plan_repository = plan_repository

    async def generate(
        self,
        *,
        actor: ActorContext,
        session_id: str,
        command: CollectionPlanGenerateRequest,
        idempotency_key: str,
    ) -> CollectionPlanCreateResult:
        """生成并持久化初始采集计划。"""

        self._require_role(actor)
        normalized_key = self._normalize_idempotency_key(idempotency_key)
        diagnosis_session = await self._session_repository.get_by_id_for_update(session_id, actor.tenant_id)
        if diagnosis_session is None:
            raise DiagnosisError(
                code="DIAGNOSIS_SESSION_NOT_FOUND",
                message="诊断会话不存在",
                http_status=404,
            )

        request_hash = self._request_hash(command)
        existing = await self._plan_repository.get_by_session_sequence(session_id, actor.tenant_id, 0)
        if existing is not None:
            if existing.request_hash != request_hash:
                raise DiagnosisError(
                    code="COLLECTION_PLAN_ALREADY_EXISTS",
                    message="该诊断会话已生成不同参数的初始采集计划",
                    http_status=409,
                    details={"plan_id": str(existing.plan_id)},
                )
            return CollectionPlanCreateResult(
                entity=existing,
                items=await self._plan_repository.list_items(existing.plan_id),
                created=False,
            )

        if DiagnosisSessionStatus(diagnosis_session.status) != DiagnosisSessionStatus.CREATED:
            raise DiagnosisError(
                code="INVALID_SESSION_STATE",
                message="仅 created 状态可以生成初始采集计划",
                http_status=409,
                details={"status": str(diagnosis_session.status)},
            )

        snapshot = await self._load_profile(diagnosis_session.selected_scenario)
        profile = CollectionProfileDefinition.model_validate(snapshot.content)
        self._validate_product_version(profile, command.product_version)
        kbd_ruleset, kbd_ruleset_checksum = await self._load_kbd_ruleset_snapshot(
            diagnosis_session.selected_category,
            kbd_dependencies=[item for item in snapshot.dependencies if item.get("resource_type") == "kbd"],
        )
        trace_id = get_current_trace_id() or secrets.token_hex(16)
        item_values, summary = self.expand_profile(
            profile=profile,
            diagnosis_session=diagnosis_session,
            context=command.context,
            trace_id=trace_id,
        )
        await self._freeze_collector_revisions(item_values, snapshot, product_version=command.product_version)
        plan_values = {
            "plan_id": uuid.uuid4(),
            "session_id": diagnosis_session.session_id,
            "tenant_id": actor.tenant_id,
            "created_by": actor.user_id,
            "plan_sequence": 0,
            "plan_revision": 1,
            "profile_name": snapshot.resource_name,
            "profile_revision": snapshot.revision,
            "profile_version": snapshot.version,
            "profile_checksum": snapshot.checksum,
            "product_version": command.product_version,
            "kbd_ruleset_snapshot": kbd_ruleset,
            "kbd_ruleset_checksum": kbd_ruleset_checksum,
            "context_snapshot": command.context,
            "required_permissions": summary["required_permissions"],
            "sensitive_data_types": summary["sensitive_data_types"],
            "unresolved_variables": summary["unresolved_variables"],
            "estimated_size_mb": summary["estimated_size_mb"],
            "estimated_duration_seconds": summary["estimated_duration_seconds"],
            "status": "ready",
            "idempotency_key": normalized_key,
            "request_hash": request_hash,
            "trace_id": trace_id,
        }
        result = await self._plan_repository.create_with_items_idempotent(plan_values, item_values)
        if not result.created and (
            result.entity.request_hash != request_hash
            or str(result.entity.session_id) != str(diagnosis_session.session_id)
        ):
            raise DiagnosisError(
                code="IDEMPOTENCY_KEY_REUSED",
                message="同一 Idempotency-Key 已用于不同请求",
                http_status=409,
                details={"plan_id": str(result.entity.plan_id)},
            )

        if result.created:
            diagnosis_session.status = DiagnosisSessionStatus.PLAN_READY
            diagnosis_session.version += 1
            await self._session_repository.flush(diagnosis_session)
            await DynamicResourceLoader(self._session).audit_usage(
                snapshot,
                UsageRecord(
                    consumer="diagnosis-service:collection-plan",
                    status=UsageStatus.SUCCESS,
                    case_id=diagnosis_session.case_id,
                    trace_id=trace_id,
                    input_payload=command.model_dump(mode="json"),
                    output_payload={"plan_id": str(result.entity.plan_id), "item_count": len(result.items)},
                    metadata={"session_id": str(diagnosis_session.session_id)},
                ),
            )

        logger.info(
            event="collection_plan_created" if result.created else "collection_plan_replayed",
            session_id=str(diagnosis_session.session_id),
            plan_id=str(result.entity.plan_id),
            profile_revision=snapshot.revision,
            item_count=len(result.items),
            trace_id=trace_id,
        )
        return result

    async def list_managed(
        self,
        *,
        actor: ActorContext,
        status: str | None = None,
        session_id: str | None = None,
        limit: int = 200,
    ) -> list[CollectionPlanCreateResult]:
        """列出租户内采集计划及其不可变执行项。"""

        self._require_role(actor)
        plans = await self._plan_repository.list_for_tenant(
            actor.tenant_id,
            status=status,
            session_id=session_id,
            limit=limit,
        )
        return [
            CollectionPlanCreateResult(
                entity=plan,
                items=await self._plan_repository.list_items(plan.plan_id),
                created=False,
            )
            for plan in plans
        ]

    async def regenerate(
        self,
        *,
        actor: ActorContext,
        plan_id: str,
        command: CollectionPlanRegenerateRequest,
        idempotency_key: str,
    ) -> CollectionPlanCreateResult:
        """以最新画像和 KBD 规则集生成新修订，并作废旧计划及其可下载制品。"""

        if not actor.has_any_role("platform_admin"):
            raise DiagnosisError(code="FORBIDDEN", message="当前角色无权重生成采集计划", http_status=403)
        normalized_key = self._normalize_idempotency_key(idempotency_key)
        replay = await self._plan_repository.get_by_idempotency_key(actor.tenant_id, normalized_key)
        if replay is not None:
            if str((replay.context_snapshot or {}).get("regenerated_from_plan_id")) != str(plan_id):
                raise DiagnosisError(
                    code="IDEMPOTENCY_KEY_REUSED",
                    message="同一 Idempotency-Key 已用于不同计划重生成请求",
                    http_status=409,
                )
            return CollectionPlanCreateResult(
                entity=replay,
                items=await self._plan_repository.list_items(replay.plan_id),
                created=False,
            )
        old_plan = await self._plan_repository.get_by_id_for_tenant(plan_id, actor.tenant_id)
        if old_plan is None:
            raise DiagnosisError(code="COLLECTION_PLAN_NOT_FOUND", message="采集计划不存在", http_status=404)
        if old_plan.status != "ready":
            raise DiagnosisError(code="COLLECTION_PLAN_NOT_READY", message="只能重生成当前生效计划", http_status=409)
        diagnosis_session = await self._session_repository.get_by_id_for_update(
            old_plan.session_id,
            actor.tenant_id,
        )
        if diagnosis_session is None:
            raise DiagnosisError(code="DIAGNOSIS_SESSION_NOT_FOUND", message="诊断会话不存在", http_status=404)
        old_plan = await self._plan_repository.get_by_id_for_update(plan_id, actor.tenant_id)
        if old_plan is None:
            raise DiagnosisError(code="COLLECTION_PLAN_NOT_FOUND", message="采集计划不存在", http_status=404)
        if old_plan.status != "ready":
            raise DiagnosisError(code="COLLECTION_PLAN_NOT_READY", message="只能重生成当前生效计划", http_status=409)
        if DiagnosisSessionStatus(diagnosis_session.status) not in {
            DiagnosisSessionStatus.PLAN_READY,
            DiagnosisSessionStatus.COLLECTING,
            DiagnosisSessionStatus.SUPPLEMENT_REQUIRED,
        }:
            raise DiagnosisError(
                code="COLLECTION_PLAN_REGENERATION_NOT_ALLOWED",
                message="当前诊断阶段不允许替换采集计划",
                http_status=409,
                details={"status": str(diagnosis_session.status)},
            )

        snapshot = await self._load_profile(diagnosis_session.selected_scenario)
        profile = CollectionProfileDefinition.model_validate(snapshot.content)
        self._validate_product_version(profile, old_plan.product_version)
        trace_id = get_current_trace_id() or secrets.token_hex(16)
        item_values, summary = self.expand_profile(
            profile=profile,
            diagnosis_session=diagnosis_session,
            context=dict(old_plan.context_snapshot or {}),
            trace_id=trace_id,
        )
        await self._freeze_collector_revisions(item_values, snapshot, product_version=old_plan.product_version)
        kbd_ruleset, kbd_ruleset_checksum = await self._load_kbd_ruleset_snapshot(
            diagnosis_session.selected_category,
            kbd_dependencies=[item for item in snapshot.dependencies if item.get("resource_type") == "kbd"],
        )
        next_revision = await self._plan_repository.next_revision(
            old_plan.session_id,
            old_plan.plan_sequence,
        )
        old_plan.status = "superseded"
        await self._session.execute(
            text(
                """
                UPDATE collector_artifact
                SET status = 'revoked',
                    revoked_at = :revoked_at,
                    revoked_by = :revoked_by,
                    revocation_reason = :reason,
                    revoked_trace_id = :trace_id,
                    updated_at = CURRENT_TIMESTAMP
                WHERE collection_plan_id = :plan_id
                  AND tenant_id = :tenant_id
                  AND status = 'ready'
                """
            ),
            {
                "revoked_at": datetime.now(UTC),
                "revoked_by": actor.user_id,
                "reason": "plan_regenerated",
                "trace_id": trace_id,
                "plan_id": old_plan.plan_id,
                "tenant_id": actor.tenant_id,
            },
        )
        request_hash = hashlib.sha256(
            f"{old_plan.plan_id}:{next_revision}:{kbd_ruleset_checksum}:{snapshot.checksum}".encode()
        ).hexdigest()
        result = await self._plan_repository.create_with_items_idempotent(
            {
                "plan_id": uuid.uuid4(),
                "session_id": old_plan.session_id,
                "tenant_id": actor.tenant_id,
                "created_by": actor.user_id,
                "plan_sequence": old_plan.plan_sequence,
                "plan_revision": next_revision,
                "profile_name": snapshot.resource_name,
                "profile_revision": snapshot.revision,
                "profile_version": snapshot.version,
                "profile_checksum": snapshot.checksum,
                "product_version": old_plan.product_version,
                "kbd_ruleset_snapshot": kbd_ruleset,
                "kbd_ruleset_checksum": kbd_ruleset_checksum,
                "context_snapshot": {
                    **dict(old_plan.context_snapshot or {}),
                    "regenerated_from_plan_id": str(old_plan.plan_id),
                    "regeneration_reason": command.reason.strip(),
                },
                "required_permissions": summary["required_permissions"],
                "sensitive_data_types": summary["sensitive_data_types"],
                "unresolved_variables": summary["unresolved_variables"],
                "estimated_size_mb": summary["estimated_size_mb"],
                "estimated_duration_seconds": summary["estimated_duration_seconds"],
                "status": "ready",
                "idempotency_key": normalized_key,
                "request_hash": request_hash,
                "trace_id": trace_id,
            },
            item_values,
        )
        logger.info(
            event="collection_plan_regenerated",
            old_plan_id=str(old_plan.plan_id),
            plan_id=str(result.entity.plan_id),
            plan_revision=next_revision,
            trace_id=trace_id,
        )
        return result

    async def get(self, *, actor: ActorContext, session_id: str, plan_id: str) -> CollectionPlanCreateResult:
        """读取本租户且属于指定会话的采集计划。"""

        self._require_role(actor)
        plan = await self._plan_repository.get_by_id_for_tenant(plan_id, actor.tenant_id)
        if plan is None or str(plan.session_id) != str(session_id):
            raise DiagnosisError(code="COLLECTION_PLAN_NOT_FOUND", message="采集计划不存在", http_status=404)
        return CollectionPlanCreateResult(
            entity=plan,
            items=await self._plan_repository.list_items(plan.plan_id),
            created=False,
        )

    @staticmethod
    def expand_profile(
        *,
        profile: CollectionProfileDefinition,
        diagnosis_session,
        context: dict[str, Any],
        trace_id: str,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """确定性展开画像；deep_dive（深度采集）以 deferred（待激活）状态保留。"""

        item_values: list[dict[str, Any]] = []
        unresolved: set[str] = set()
        for profile_item in profile.items:
            if profile_item.required_level == CollectionRequirementLevel.CONDITIONAL and not _matches_condition(
                profile_item.condition,
                context,
            ):
                continue
            targets, missing = _resolve_targets(profile_item, diagnosis_session)
            unresolved.update(missing)
            for target in targets:
                item_values.append(
                    {
                        "sequence": len(item_values) + 1,
                        "collector_id": profile_item.collector_id,
                        "display_name": profile_item.display_name,
                        "required_level": profile_item.required_level.value,
                        "activation_state": (
                            "deferred"
                            if profile_item.required_level == CollectionRequirementLevel.DEEP_DIVE
                            else "active"
                        ),
                        "target": target,
                        "time_window": {
                            "start_time": (
                                diagnosis_session.incident_start_time
                                - timedelta(minutes=profile_item.time_window.before_minutes)
                            ).isoformat(),
                            "end_time": (
                                diagnosis_session.incident_end_time
                                + timedelta(minutes=profile_item.time_window.after_minutes)
                            ).isoformat(),
                            "timezone": diagnosis_session.incident_timezone,
                        },
                        "collector_parameters": dict(profile_item.parameters),
                        "condition_snapshot": (
                            profile_item.condition.model_dump(mode="json") if profile_item.condition else None
                        ),
                        "reason": profile_item.reason,
                        "expected_size_mb": profile_item.expected_size_mb,
                        "timeout_seconds": profile_item.timeout_seconds,
                        "required_permissions": sorted(set(profile_item.required_permissions)),
                        "sensitive_data_types": sorted(set(profile_item.sensitive_data_types)),
                        "trace_id": trace_id,
                    }
                )

        return item_values, {
            "required_permissions": sorted(
                {permission for item in item_values for permission in item["required_permissions"]}
            ),
            "sensitive_data_types": sorted(
                {data_type for item in item_values for data_type in item["sensitive_data_types"]}
            ),
            "unresolved_variables": sorted(unresolved),
            "estimated_size_mb": round(
                sum(item["expected_size_mb"] for item in item_values if item["activation_state"] == "active"),
                2,
            ),
            "estimated_duration_seconds": sum(
                item["timeout_seconds"] for item in item_values if item["activation_state"] == "active"
            ),
        }

    async def _load_profile(self, profile_name: str):
        """加载会话场景对应的生效采集画像。"""

        try:
            return await DynamicResourceLoader(self._session).get_active(
                COLLECTION_PROFILE_RESOURCE_TYPE,
                profile_name,
            )
        except ResourceNotFoundError as exc:
            raise DiagnosisError(
                code="COLLECTION_PROFILE_NOT_FOUND",
                message="当前故障场景尚未发布采集画像",
                http_status=409,
                details={"profile_id": profile_name},
            ) from exc

    async def _freeze_collector_revisions(
        self,
        item_values: list[dict[str, Any]],
        profile_snapshot,
        *,
        product_version: str,
    ) -> None:
        """把画像审批时引用的 Collector 修订冻结到每个计划项。"""

        dependencies = {
            str(item.get("resource_name")): item
            for item in profile_snapshot.dependencies
            if item.get("resource_type") == "collector" and item.get("resource_name")
        }
        loader = DynamicResourceLoader(self._session)
        snapshots: dict[str, Any] = {}
        for collector_id in sorted({str(item["collector_id"]) for item in item_values}):
            dependency = dependencies.get(collector_id)
            try:
                snapshot = (
                    await loader.get_revision("collector", collector_id, int(dependency["revision"]))
                    if dependency and dependency.get("revision") is not None
                    else await loader.get_active("collector", collector_id)
                )
            except ResourceNotFoundError as exc:
                raise DiagnosisError(
                    code="COLLECTION_PLAN_COLLECTOR_REVISION_MISSING",
                    message="采集画像引用的 Collector 修订不存在，必须重新同步或审批画像",
                    http_status=409,
                    details={"collector_id": collector_id, "profile_revision": profile_snapshot.revision},
                ) from exc
            if dependency and dependency.get("checksum") not in (None, snapshot.checksum):
                raise DiagnosisError(
                    code="COLLECTION_PLAN_COLLECTOR_CHECKSUM_MISMATCH",
                    message="采集画像中的 Collector 依赖校验和不一致",
                    http_status=409,
                    details={"collector_id": collector_id, "collector_revision": snapshot.revision},
                )
            try:
                definition = CollectorDefinitionWrite.model_validate(snapshot.content)
            except ValidationError as exc:
                raise DiagnosisError(
                    code="COLLECTION_PLAN_COLLECTOR_REVISION_INVALID",
                    message="采集画像引用的 Collector 修订不符合运行时契约，必须重新同步并发布画像",
                    http_status=409,
                    details={"collector_id": collector_id, "collector_revision": snapshot.revision},
                ) from exc
            if not matches_any_product_version(product_version, definition.supported_product_versions):
                raise DiagnosisError(
                    code="COLLECTION_PLAN_COLLECTOR_VERSION_UNSUPPORTED",
                    message=(
                        f"采集项“{definition.display_name}”不支持现场产品版本 {product_version}，"
                        f"仅支持：{'、'.join(definition.supported_product_versions)}"
                    ),
                    http_status=422,
                    details={
                        "collector_id": collector_id,
                        "product_version": product_version,
                        "supported_product_versions": definition.supported_product_versions,
                    },
                )
            snapshots[collector_id] = snapshot

        for item in item_values:
            snapshot = snapshots[str(item["collector_id"])]
            item["collector_revision"] = snapshot.revision
            item["collector_version"] = snapshot.version
            item["collector_checksum"] = snapshot.checksum

    async def _load_kbd_ruleset_snapshot(
        self,
        category_id: str | None,
        *,
        kbd_dependencies: list[dict[str, Any]] | None = None,
    ) -> tuple[list[dict[str, Any]], str]:
        """固化当前可用 KBD 精确集合，确保离线结果可复现。"""

        mapping_result = await self._session.execute(
            text(
                """
                SELECT mapping_id, source_kbd_id, source_kbd_revision, source_signal_id,
                       execution_contract_checksum, acquire_tool, category_scope, command_scope,
                       collector_id, query_type, field_mapping, priority, updated_at
                FROM offline_signal_collector_mapping
                WHERE is_enabled = true AND source_kbd_id IS NOT NULL
                ORDER BY source_kbd_id, source_kbd_revision, source_signal_id, priority, collector_id
                """
            )
        )
        mappings = [dict(item) for item in mapping_result.mappings().all()]
        dependencies = sorted(
            (
                item
                for item in (kbd_dependencies or [])
                if item.get("resource_name") and item.get("revision") is not None
            ),
            key=lambda item: (str(item["resource_name"]), int(item["revision"])),
        )
        if dependencies:
            snapshot: list[dict[str, Any]] = []
            loader = DynamicResourceLoader(self._session)
            for dependency in dependencies:
                try:
                    resource = await loader.get_revision(
                        "kbd",
                        str(dependency["resource_name"]),
                        int(dependency["revision"]),
                    )
                except ResourceNotFoundError as exc:
                    raise DiagnosisError(
                        code="COLLECTION_PLAN_KBD_REVISION_MISSING",
                        message="采集画像引用的 KBD 修订不存在，必须重新同步或审批画像",
                        http_status=409,
                        details={
                            "kbd_id": dependency["resource_name"],
                            "kbd_revision": dependency["revision"],
                        },
                    ) from exc
                if dependency.get("checksum") not in {None, resource.checksum}:
                    raise DiagnosisError(
                        code="COLLECTION_PLAN_KBD_CHECKSUM_MISMATCH",
                        message="采集画像引用的 KBD 校验和不一致",
                        http_status=409,
                        details={"kbd_id": dependency["resource_name"], "kbd_revision": resource.revision},
                    )
                row = resource.content
                signal_document = row.get("signals_json") or []
                signals = signal_document.get("signals", []) if isinstance(signal_document, dict) else signal_document
                row_category = row.get("category_id")
                content = {
                    "kbd_id": int(row.get("id") or resource.resource_name),
                    "support_id": row.get("support_id"),
                    "title": row.get("title"),
                    "category_id": row_category,
                    "resource_name": resource.resource_name,
                    "revision": resource.revision,
                    "version": resource.version,
                    "resource_checksum": resource.checksum,
                    "signals": list(signals),
                    "verification_contract": signal_document.get("verification_contract", {})
                    if isinstance(signal_document, dict)
                    else {},
                    "generation_metadata": signal_document.get("generation_metadata", {})
                    if isinstance(signal_document, dict)
                    else {},
                    "publish_validation": signal_document.get("publish_validation", {})
                    if isinstance(signal_document, dict)
                    else {},
                    "root_cause": row.get("root_cause"),
                    "solution": row.get("solution"),
                    "operational_impact": row.get("operational_impact"),
                    "recommendations": row.get("recommendations"),
                    "metadata": resource.contract.get("metadata") or {},
                    "offline_signal_mappings": self._freeze_kbd_mappings(
                        mappings,
                        int(row.get("id") or resource.resource_name),
                        resource.revision,
                    ),
                    "updated_at": resource.published_at.isoformat() if resource.published_at else None,
                }
                content["checksum"] = hashlib.sha256(
                    json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
                ).hexdigest()
                snapshot.append(content)
            canonical = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            return snapshot, hashlib.sha256(canonical.encode("utf-8")).hexdigest()

        result = await self._session.execute(
            text(
                """
                SELECT e.id, e.support_id, e.title, e.category_id, e.signals_json, e.updated_at,
                       e.root_cause, e.solution, e.operational_impact, e.recommendations, e.metadata,
                       r.revision AS resource_revision, r.version AS resource_version,
                       r.checksum AS resource_checksum
                FROM kbd_entry e
                JOIN dynamic_resource_active a
                  ON a.resource_type = 'kbd' AND a.resource_name = e.id::text
                JOIN dynamic_resource_revision r
                  ON r.resource_type = a.resource_type
                 AND r.resource_name = a.resource_name
                 AND r.revision = a.active_revision
                WHERE e.status = 'published'
                  AND r.status = 'published'
                  AND jsonb_array_length(
                      CASE WHEN jsonb_typeof(e.signals_json) = 'array'
                           THEN e.signals_json
                           ELSE COALESCE(e.signals_json->'signals', '[]'::jsonb)
                      END
                  ) > 0
                  AND (CAST(:category_id AS varchar) IS NULL OR e.category_id = CAST(:category_id AS varchar))
                ORDER BY e.support_id, e.id
                """
            ),
            {"category_id": category_id},
        )
        snapshot: list[dict[str, Any]] = []
        for row in result.mappings().all():
            signal_document = row["signals_json"] or []
            signals = signal_document.get("signals", []) if isinstance(signal_document, dict) else signal_document
            content = {
                "kbd_id": int(row["id"]),
                "support_id": row["support_id"],
                "title": row["title"],
                "category_id": row["category_id"],
                "resource_name": str(row["id"]),
                "revision": row["resource_revision"],
                "version": row["resource_version"],
                "resource_checksum": row["resource_checksum"],
                "signals": list(signals),
                "verification_contract": signal_document.get("verification_contract", {})
                if isinstance(signal_document, dict)
                else {},
                "generation_metadata": signal_document.get("generation_metadata", {})
                if isinstance(signal_document, dict)
                else {},
                "publish_validation": signal_document.get("publish_validation", {})
                if isinstance(signal_document, dict)
                else {},
                "root_cause": row["root_cause"],
                "solution": row["solution"],
                "operational_impact": row["operational_impact"],
                "recommendations": row["recommendations"],
                "metadata": row["metadata"] or {},
                "offline_signal_mappings": self._freeze_kbd_mappings(
                    mappings,
                    int(row["id"]),
                    int(row["resource_revision"]),
                ),
                "updated_at": row["updated_at"].isoformat(),
            }
            content["checksum"] = hashlib.sha256(
                json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            snapshot.append(content)
        # 存量会话可能保存分类名称而 KBD 使用分类编码；无精确命中时冻结全量可用规则，
        # 不能用空规则集继续生成一个表面可用、实际无法诊断的计划。
        if not snapshot and category_id is not None:
            return await self._load_kbd_ruleset_snapshot(None)
        canonical = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return snapshot, hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _freeze_kbd_mappings(mappings: list[dict[str, Any]], kbd_id: int, kbd_revision: int) -> list[dict[str, Any]]:
        """只冻结精确绑定当前 KBD 修订的 Signal 映射。"""

        return [
            {
                **{key: value for key, value in mapping.items() if key != "updated_at"},
                # mapping_id 是 UUID 列，须转为字符串才能进入 json.dumps 校验和计算
                "mapping_id": str(mapping["mapping_id"]),
                "updated_at": mapping["updated_at"].isoformat(),
            }
            for mapping in mappings
            if int(mapping["source_kbd_id"]) == kbd_id and int(mapping["source_kbd_revision"]) == kbd_revision
        ]

    @staticmethod
    def _validate_product_version(profile: CollectionProfileDefinition, product_version: str) -> None:
        """按画像声明的 glob/比较表达式校验产品版本。"""

        if not matches_any_product_version(product_version, profile.supported_product_versions):
            raise DiagnosisError(
                code="UNSUPPORTED_PRODUCT_VERSION",
                message="当前采集画像不支持该产品版本",
                http_status=422,
                details={
                    "product_version": product_version,
                    "supported_product_versions": profile.supported_product_versions,
                },
            )

    @staticmethod
    def _request_hash(command: CollectionPlanGenerateRequest) -> str:
        """计算计划生成请求指纹。"""

        canonical = json.dumps(
            command.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _normalize_idempotency_key(idempotency_key: str) -> str:
        """校验并规范化幂等键。"""

        normalized = idempotency_key.strip()
        if not normalized or len(normalized) > 128:
            raise DiagnosisError(
                code="INVALID_IDEMPOTENCY_KEY",
                message="Idempotency-Key 不能为空且长度不能超过 128",
                http_status=422,
            )
        return normalized

    @staticmethod
    def _require_role(actor: ActorContext) -> None:
        """校验采集计划角色。"""

        if actor.roles.isdisjoint(PLAN_ROLES):
            raise DiagnosisError(code="FORBIDDEN", message="当前角色无权操作采集计划", http_status=403)


def _matches_condition(condition: CollectionCondition | None, context: dict[str, Any]) -> bool:
    """执行受控条件匹配。"""

    if condition is None:
        return True
    present, actual = _resolve_context_value(context, condition.field)
    if condition.operator == "exists":
        return present
    if not present:
        return False
    if condition.operator == "eq":
        return actual == condition.value
    return actual in condition.value


def _resolve_context_value(context: dict[str, Any], field: str) -> tuple[bool, Any]:
    """读取点分隔上下文字段。"""

    current: Any = context
    for part in field.split("."):
        if not isinstance(current, dict) or part not in current:
            return False, None
        current = current[part]
    return True, current


def _resolve_targets(profile_item: CollectionProfileItem, diagnosis_session) -> tuple[list[dict[str, Any]], set[str]]:
    """根据画像目标范围展开执行目标。"""

    if profile_item.target_scope == "once":
        return [{"type": "diagnosis_session", "id": str(diagnosis_session.session_id)}], set()
    if profile_item.target_scope == "affected_object":
        affected_objects = [dict(item) for item in diagnosis_session.affected_objects if item.get("id")]
        if affected_objects:
            return affected_objects, set()
        # 混合画像可同时覆盖“已有对象异常”和“对象创建失败”。对象尚不存在时，
        # 对象级采集项对本会话不适用，不得生成空 target_id 命令。
        return [], set()

    source_nodes = sorted(
        {str(item.get("source_node")).strip() for item in diagnosis_session.affected_objects if item.get("source_node")}
    )
    if source_nodes:
        return [{"type": "node", "id": node} for node in source_nodes], set()
    return [{"type": "variable", "id": "source_node"}], {"source_node"}
