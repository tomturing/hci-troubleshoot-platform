"""Linux Collector Artifact（采集器制品）生成服务。"""

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
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import ActorContext
from app.domain.collector_security import render_collector_command
from app.domain.session_state import DiagnosisSessionStatus
from app.domain.signed_document import attach_detached_signature
from app.errors import DiagnosisError
from app.models.collector_definition import CollectorDefinition
from app.repositories.collection_plan_repository import CollectionPlanRepository
from app.repositories.collector_artifact_repository import (
    CollectorArtifactCreateResult,
    CollectorArtifactRepository,
)
from app.repositories.diagnosis_session_repository import DiagnosisSessionRepository
from app.schemas.collector_artifact import CollectorArtifactGenerateRequest, CollectorArtifactRevokeRequest
from app.schemas.collector_definition import CollectorDefinitionWrite
from app.services.artifact_signer import ArtifactSigner
from app.services.collector_definition_service import COLLECTOR_RESOURCE_TYPE
from app.services.envelope_encryption import EnvelopeEncryptionService

logger = get_logger("collector-artifact-service")
ARTIFACT_ROLES = frozenset({"platform_admin", "support_engineer", "diagnosis_worker"})


class CollectorArtifactService:
    """解析已审批 Collector 并生成离线 Linux 编排制品。"""

    def __init__(
        self,
        *,
        session: AsyncSession,
        session_repository: DiagnosisSessionRepository,
        plan_repository: CollectionPlanRepository,
        artifact_repository: CollectorArtifactRepository,
        signer: ArtifactSigner | None,
        envelope_encryption: EnvelopeEncryptionService | None = None,
    ):
        self._session = session
        self._session_repository = session_repository
        self._plan_repository = plan_repository
        self._artifact_repository = artifact_repository
        self._signer = signer
        self._envelope_encryption = envelope_encryption

    async def generate(
        self,
        *,
        actor: ActorContext,
        session_id: str,
        command: CollectorArtifactGenerateRequest,
        idempotency_key: str,
    ) -> CollectorArtifactCreateResult:
        """生成签名结构化制品，可编排直接命令、HCI API 和人工附件指引。"""

        self._require_role(actor)
        normalized_key = self._normalize_idempotency_key(idempotency_key)
        diagnosis_session = await self._session_repository.get_by_id_for_update(session_id, actor.tenant_id)
        if diagnosis_session is None:
            raise DiagnosisError(code="DIAGNOSIS_SESSION_NOT_FOUND", message="诊断会话不存在", http_status=404)
        if DiagnosisSessionStatus(diagnosis_session.status) not in {
            DiagnosisSessionStatus.PLAN_READY,
            DiagnosisSessionStatus.COLLECTING,
        }:
            raise DiagnosisError(
                code="INVALID_SESSION_STATE",
                message="当前诊断会话状态不能生成采集器制品",
                http_status=409,
                details={"status": str(diagnosis_session.status)},
            )

        plan = await self._plan_repository.get_by_id_for_tenant(command.collection_plan_id, actor.tenant_id)
        if plan is None or str(plan.session_id) != str(session_id):
            raise DiagnosisError(code="COLLECTION_PLAN_NOT_FOUND", message="采集计划不存在", http_status=404)
        if plan.status != "ready":
            raise DiagnosisError(
                code="COLLECTION_PLAN_NOT_READY",
                message="采集计划已失效，不能生成制品",
                http_status=409,
            )
        plan_items = await self._plan_repository.list_items(plan.plan_id)
        selected_items, target_key = self._select_items(plan_items, command.target_node)
        selected_item_ids = {str(item.item_id) for item in selected_items}
        unknown_parameter_items = sorted(set(command.parameters_by_item) - selected_item_ids)
        if unknown_parameter_items:
            raise DiagnosisError(
                code="UNKNOWN_PLAN_ITEM_PARAMETERS",
                message="parameters_by_item 包含不属于当前制品的计划项",
                http_status=422,
                details={"plan_item_ids": unknown_parameter_items},
            )
        request_hash = self._request_hash(command, target_key)

        if self._signer is None:
            raise DiagnosisError(
                code="ARTIFACT_SIGNER_UNAVAILABLE",
                message="Collector Artifact 签名器尚未配置",
                http_status=503,
            )
        if self._envelope_encryption is None:
            raise DiagnosisError(
                code="ARTIFACT_ENCRYPTION_UNAVAILABLE",
                message="证据包加密密钥尚未配置，不能生成采集器制品",
                http_status=503,
            )

        existing = await self._artifact_repository.get_by_plan_target(plan.plan_id, actor.tenant_id, target_key)
        if existing is not None:
            encryption = dict(existing.manifest_json or {}).get("bundle_encryption")
            if (
                existing.status != "ready"
                or existing.expires_at <= datetime.now(UTC)
                or not isinstance(encryption, dict)
                or not encryption.get("public_key_pem_base64")
            ):
                raise DiagnosisError(
                    code="COLLECTOR_ARTIFACT_REGENERATION_REQUIRED",
                    message="现有制品已失效或缺少加密公钥，请重生成采集计划后再生成制品",
                    http_status=409,
                    details={
                        "artifact_id": str(existing.artifact_id),
                        "collection_plan_id": str(plan.plan_id),
                    },
                )
            if existing.request_hash != request_hash:
                raise DiagnosisError(
                    code="COLLECTOR_ARTIFACT_ALREADY_EXISTS",
                    message="该采集计划目标已生成不同参数的制品",
                    http_status=409,
                    details={"artifact_id": str(existing.artifact_id)},
                )
            return CollectorArtifactCreateResult(
                entity=existing,
                items=await self._artifact_repository.list_items(existing.artifact_id),
                created=False,
            )

        trace_id = get_current_trace_id() or secrets.token_hex(16)
        resolved_items = await self._resolve_collectors(
            selected_items=selected_items,
            product_version=plan.product_version,
            parameters_by_item=command.parameters_by_item,
            target_key=target_key,
            trace_id=trace_id,
            case_id=diagnosis_session.case_id,
        )
        artifact_id = uuid.uuid4()
        artifact_document = self._build_artifact_document(
            artifact_id=str(artifact_id),
            session_id=str(diagnosis_session.session_id),
            collection_plan_id=str(plan.plan_id),
            target_key=target_key,
            resolved_items=resolved_items,
            kbd_ruleset_snapshot=list(getattr(plan, "kbd_ruleset_snapshot", None) or []),
        )
        content_text = json.dumps(
            artifact_document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ) + "\n"
        content = content_text.encode("utf-8")
        artifact_sha256 = hashlib.sha256(content).hexdigest()
        signature = self._signer.sign(content)
        signed_at = datetime.now(UTC)
        expires_at = signed_at + timedelta(hours=command.expires_in_hours)
        file_name = f"collector_{str(diagnosis_session.session_id)[:8]}_{target_key}.hci-collector.json"
        manifest = {
            "schema_version": "1.2",
            "artifact_id": str(artifact_id),
            "artifact_type": "structured_collector",
            "session_id": str(diagnosis_session.session_id),
            "collection_plan_id": str(plan.plan_id),
            "target_key": target_key,
            "file_name": file_name,
            "product_version": plan.product_version,
            "profile": {
                "name": plan.profile_name,
                "revision": plan.profile_revision,
                "version": plan.profile_version,
                "checksum": plan.profile_checksum,
            },
            "kbd_ruleset": {
                "checksum": getattr(plan, "kbd_ruleset_checksum", ""),
                "entries": list(getattr(plan, "kbd_ruleset_snapshot", []) or []),
            },
            "bundle_encryption": self._envelope_encryption.public_metadata(),
            "artifact_sha256": artifact_sha256,
            "signature": {
                "algorithm": signature.algorithm,
                "key_id": signature.key_id,
                "signature_base64": signature.signature_base64,
                "public_key_fingerprint": signature.public_key_fingerprint,
                "public_key_base64": signature.public_key_base64,
                "signed_at": signed_at.isoformat(),
                "expires_at": expires_at.isoformat(),
            },
            "collection_items": [
                {
                    "plan_item_id": str(item["plan_item"].item_id),
                    "collector_id": item["snapshot"].resource_name,
                    "collector_revision": item["snapshot"].revision,
                    "collector_checksum": item["snapshot"].checksum,
                    "platform": item["definition"].platform,
                    "executor": item["execution_spec"]["executor"],
                    "target": item["plan_item"].target,
                    "time_window": item["plan_item"].time_window,
                    "output_contract": item["definition"].output_contract.model_dump(mode="json"),
                }
                for item in resolved_items
            ],
        }
        manifest = attach_detached_signature(manifest, self._signer)
        artifact_values = {
            "artifact_id": artifact_id,
            "session_id": diagnosis_session.session_id,
            "collection_plan_id": plan.plan_id,
            "tenant_id": actor.tenant_id,
            "created_by": actor.user_id,
            "target_key": target_key,
            "artifact_type": "structured_collector",
            "schema_version": "1.2",
            "file_name": file_name,
            "content_text": content_text,
            "artifact_sha256": artifact_sha256,
            "signature_algorithm": signature.algorithm,
            "signature_base64": signature.signature_base64,
            "signing_key_id": signature.key_id,
            "public_key_base64": signature.public_key_base64,
            "public_key_fingerprint": signature.public_key_fingerprint,
            "signed_at": signed_at,
            "expires_at": expires_at,
            "manifest_json": manifest,
            "status": "ready",
            "idempotency_key": normalized_key,
            "request_hash": request_hash,
            "trace_id": trace_id,
        }
        item_values = [
            {
                "plan_item_id": item["plan_item"].item_id,
                "sequence": index,
                "collector_id": item["snapshot"].resource_name,
                "collector_revision": item["snapshot"].revision,
                "collector_checksum": item["snapshot"].checksum,
                "rendered_command": item["rendered_command"],
                "output_contract": item["definition"].output_contract.model_dump(mode="json"),
                "timeout_seconds": item["definition"].timeout_seconds,
                "max_output_bytes": int(float(item["definition"].max_output_mb) * 1024 * 1024),
                "trace_id": trace_id,
            }
            for index, item in enumerate(resolved_items, start=1)
        ]
        result = await self._artifact_repository.create_with_items_idempotent(artifact_values, item_values)
        if not result.created and (
            result.entity.request_hash != request_hash or str(result.entity.collection_plan_id) != str(plan.plan_id)
        ):
            raise DiagnosisError(
                code="IDEMPOTENCY_KEY_REUSED",
                message="同一 Idempotency-Key 已用于不同制品请求",
                http_status=409,
            )
        if result.created and DiagnosisSessionStatus(diagnosis_session.status) == DiagnosisSessionStatus.PLAN_READY:
            diagnosis_session.status = DiagnosisSessionStatus.COLLECTING
            diagnosis_session.version += 1
            await self._session_repository.flush(diagnosis_session)

        logger.info(
            event="collector_artifact_created" if result.created else "collector_artifact_replayed",
            artifact_id=str(result.entity.artifact_id),
            session_id=str(diagnosis_session.session_id),
            collection_plan_id=str(plan.plan_id),
            target_key=target_key,
            item_count=len(result.items),
            trace_id=trace_id,
        )
        return result

    async def get(self, *, actor: ActorContext, session_id: str, artifact_id: str) -> CollectorArtifactCreateResult:
        """读取本租户制品元数据。"""

        self._require_role(actor)
        artifact = await self._artifact_repository.get_by_id_for_tenant(artifact_id, actor.tenant_id)
        if artifact is None or str(artifact.session_id) != str(session_id):
            raise DiagnosisError(code="COLLECTOR_ARTIFACT_NOT_FOUND", message="采集器制品不存在", http_status=404)
        return CollectorArtifactCreateResult(
            entity=artifact,
            items=await self._artifact_repository.list_items(artifact.artifact_id),
            created=False,
        )

    async def list_managed(
        self,
        *,
        actor: ActorContext,
        status: str | None = None,
        session_id: str | None = None,
        limit: int = 200,
    ) -> list[CollectorArtifactCreateResult]:
        """列出租户内 Collector Artifact（采集器制品）。"""

        self._require_role(actor)
        artifacts = await self._artifact_repository.list_for_tenant(
            actor.tenant_id,
            status=status,
            session_id=session_id,
            limit=limit,
        )
        return [
            CollectorArtifactCreateResult(
                entity=artifact,
                items=await self._artifact_repository.list_items(artifact.artifact_id),
                created=False,
            )
            for artifact in artifacts
        ]

    async def revoke_managed(
        self,
        *,
        actor: ActorContext,
        artifact_id: str,
        command: CollectorArtifactRevokeRequest,
    ) -> CollectorArtifactCreateResult:
        """管理端按制品 ID 撤销，不要求先知道会话 ID。"""

        if not actor.has_any_role("platform_admin"):
            raise DiagnosisError(code="FORBIDDEN", message="当前角色无权撤销采集器制品", http_status=403)
        artifact = await self._artifact_repository.get_by_id_for_tenant(artifact_id, actor.tenant_id)
        if artifact is None:
            raise DiagnosisError(code="COLLECTOR_ARTIFACT_NOT_FOUND", message="采集器制品不存在", http_status=404)
        return await self.revoke(
            actor=actor,
            session_id=str(artifact.session_id),
            artifact_id=artifact_id,
            command=command,
        )

    async def revoke(
        self,
        *,
        actor: ActorContext,
        session_id: str,
        artifact_id: str,
        command: CollectorArtifactRevokeRequest,
    ) -> CollectorArtifactCreateResult:
        """撤销尚未过期的制品，重复撤销保持幂等。"""

        if not actor.has_any_role("platform_admin"):
            raise DiagnosisError(code="FORBIDDEN", message="当前角色无权撤销采集器制品", http_status=403)
        artifact = await self._artifact_repository.get_by_id_for_update(artifact_id, actor.tenant_id)
        if artifact is None or str(artifact.session_id) != str(session_id):
            raise DiagnosisError(code="COLLECTOR_ARTIFACT_NOT_FOUND", message="采集器制品不存在", http_status=404)
        if artifact.status != "revoked":
            artifact.status = "revoked"
            artifact.revoked_at = datetime.now(UTC)
            artifact.revoked_by = actor.user_id
            artifact.revocation_reason = command.reason.strip()
            artifact.revoked_trace_id = get_current_trace_id() or secrets.token_hex(16)
            await self._session.flush()
        return CollectorArtifactCreateResult(
            entity=artifact,
            items=await self._artifact_repository.list_items(artifact.artifact_id),
            created=False,
        )

    async def _resolve_collectors(
        self,
        *,
        selected_items: list,
        product_version: str,
        parameters_by_item: dict[str, dict[str, Any]],
        target_key: str,
        trace_id: str,
        case_id: str,
    ) -> list[dict[str, Any]]:
        resolved: list[dict[str, Any]] = []
        loader = DynamicResourceLoader(self._session)
        for plan_item in selected_items:
            registry_result = await self._session.execute(
                select(CollectorDefinition).where(CollectorDefinition.collector_id == plan_item.collector_id)
            )
            registry_definition = registry_result.scalar_one_or_none()
            if registry_definition is None:
                raise DiagnosisError(
                    code="COLLECTOR_NOT_REGISTERED",
                    message="采集计划引用了未注册 Collector",
                    http_status=409,
                    details={"collector_id": plan_item.collector_id},
                )
            frozen_revision = getattr(plan_item, "collector_revision", None)
            # 新计划已冻结不可变修订；Tool/KBD 同步后旧 Collector 退出 active 不应改写旧计划。
            # 显式安全禁用会先将相关计划置为 invalidated，因此不需要在这里跟随可变状态。
            if frozen_revision is None and (
                registry_definition.review_status != "approved" or not registry_definition.is_enabled
            ):
                raise DiagnosisError(
                    code="COLLECTOR_NOT_APPROVED",
                    message="历史采集计划未冻结 Collector 修订，且当前 Collector 未审批或已禁用",
                    http_status=409,
                    details={"collector_id": plan_item.collector_id},
                )
            try:
                snapshot = (
                    await loader.get_revision(COLLECTOR_RESOURCE_TYPE, plan_item.collector_id, frozen_revision)
                    if frozen_revision is not None
                    else await loader.get_active(COLLECTOR_RESOURCE_TYPE, plan_item.collector_id)
                )
            except ResourceNotFoundError as exc:
                raise DiagnosisError(
                    code="COLLECTOR_REVISION_NOT_FOUND",
                    message="采集计划冻结的 Collector 修订版本不存在",
                    http_status=409,
                    details={
                        "collector_id": plan_item.collector_id,
                        "collector_revision": getattr(plan_item, "collector_revision", None),
                    },
                ) from exc
            frozen_checksum = getattr(plan_item, "collector_checksum", None)
            if frozen_checksum and frozen_checksum != snapshot.checksum:
                raise DiagnosisError(
                    code="COLLECTOR_REVISION_CHECKSUM_MISMATCH",
                    message="采集计划冻结的 Collector 校验和不一致",
                    http_status=409,
                    details={"collector_id": plan_item.collector_id, "collector_revision": snapshot.revision},
                )
            try:
                definition = CollectorDefinitionWrite.model_validate(snapshot.content)
            except ValidationError as exc:
                raise DiagnosisError(
                    code="COLLECTOR_REVISION_INVALID",
                    message="Collector 已发布修订版本不符合运行时契约",
                    http_status=409,
                    details={"collector_id": plan_item.collector_id, "revision": snapshot.revision},
                ) from exc
            if definition.risk_level != "read_only":
                raise DiagnosisError(
                    code="COLLECTOR_RISK_NOT_ALLOWED",
                    message="P0 只允许只读 Collector",
                    http_status=409,
                    details={"collector_id": plan_item.collector_id},
                )
            if not matches_any_product_version(product_version, definition.supported_product_versions):
                raise DiagnosisError(
                    code="COLLECTOR_VERSION_UNSUPPORTED",
                    message="Collector 不支持当前产品版本",
                    http_status=409,
                    details={
                        "collector_id": plan_item.collector_id,
                        "product_version": product_version,
                        "supported_product_versions": definition.supported_product_versions,
                    },
                )

            target = dict(plan_item.target or {})
            if target.get("type") == "variable" and target.get("id") == "source_node":
                target = {"type": "node", "id": target_key}
            time_window = dict(plan_item.time_window or {})
            # 画像同步固化的参数是采集语义的一部分，不能被制品生成请求临时覆盖。
            item_parameters = {
                **parameters_by_item.get(str(plan_item.item_id), {}),
                **dict(getattr(plan_item, "collector_parameters", None) or {}),
            }
            values = {
                **item_parameters,
                "target_id": target.get("id"),
                "target_type": target.get("type"),
                "window_start": time_window.get("start_time"),
                "window_end": time_window.get("end_time"),
            }
            values = {key: value for key, value in values.items() if value is not None}
            if definition.executor == "shell":
                argv, rendered_command = render_collector_command(
                    definition.command_template,
                    dict(definition.parameter_schema or {}),
                    values,
                )
                execution_spec = {"executor": "command", "argv": argv}
            elif definition.executor == "http":
                rendered_command = definition.command_template.strip()
                method, path = rendered_command.split(" ", 1)
                execution_spec = {"executor": "http", "method": method, "path": path}
            else:
                rendered_command = definition.command_template.strip()
                execution_spec = {"executor": "manual", "guide": rendered_command}
            await loader.audit_usage(
                snapshot,
                UsageRecord(
                    consumer="diagnosis-service:collector-artifact",
                    status=UsageStatus.SUCCESS,
                    case_id=case_id,
                    trace_id=trace_id,
                    input_payload={"plan_item_id": str(plan_item.item_id)},
                    output_payload={"collector_revision": snapshot.revision},
                ),
            )
            resolved.append(
                {
                    "plan_item": plan_item,
                    "definition": definition,
                    "snapshot": snapshot,
                    "rendered_command": rendered_command,
                    "execution_spec": execution_spec,
                }
            )
        return resolved

    @staticmethod
    def _select_items(plan_items: list, requested_target_node: str | None) -> tuple[list, str]:
        active_items = [item for item in plan_items if item.activation_state == "active"]
        has_unresolved_source_node = any(
            item.target.get("type") == "variable" and item.target.get("id") == "source_node" for item in active_items
        )
        source_nodes = sorted(
            {
                str(item.target.get("source_node") or item.target.get("id")).strip()
                for item in active_items
                if item.target.get("source_node") or item.target.get("type") == "node"
            }
        )
        if requested_target_node:
            target_key = requested_target_node.strip()
            if not has_unresolved_source_node and target_key not in source_nodes:
                raise DiagnosisError(
                    code="TARGET_NODE_NOT_FOUND",
                    message="指定的 target_node 不属于当前采集计划",
                    http_status=422,
                    details={"target_node": target_key, "target_nodes": source_nodes},
                )
        elif len(source_nodes) > 1 or has_unresolved_source_node:
            raise DiagnosisError(
                code="TARGET_NODE_REQUIRED",
                message="多节点采集计划必须指定 target_node",
                http_status=422,
                details={"target_nodes": source_nodes},
            )
        else:
            target_key = source_nodes[0] if source_nodes else "all"

        if target_key == "all":
            selected = active_items
        else:
            selected = [
                item
                for item in active_items
                if item.target.get("type") == "diagnosis_session"
                or item.target.get("source_node") == target_key
                or (item.target.get("type") == "node" and item.target.get("id") == target_key)
                or (item.target.get("type") == "variable" and item.target.get("id") == "source_node")
            ]
        if not selected:
            raise DiagnosisError(
                code="NO_ACTIVE_COLLECTION_ITEMS",
                message="当前目标没有可生成制品的激活采集项",
                http_status=409,
            )
        return selected, target_key

    @staticmethod
    def _build_artifact_document(
        *,
        artifact_id: str,
        session_id: str,
        collection_plan_id: str,
        target_key: str,
        resolved_items: list[dict[str, Any]],
        kbd_ruleset_snapshot: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """生成无 Shell 语义的结构化执行清单。"""

        source_refs_by_collector: dict[str, set[tuple[str, str]]] = {}
        for kbd in kbd_ruleset_snapshot or []:
            support_id = str(kbd.get("support_id") or "")
            for mapping in kbd.get("offline_signal_mappings") or []:
                collector_id = str(mapping.get("collector_id") or "")
                signal_id = str(mapping.get("source_signal_id") or "")
                if collector_id and support_id and signal_id:
                    source_refs_by_collector.setdefault(collector_id, set()).add((support_id, signal_id))

        execution_items: list[dict[str, Any]] = []
        for item in resolved_items:
            plan_item = item["plan_item"]
            definition = item["definition"]
            max_bytes = int(float(definition.max_output_mb) * 1024 * 1024)
            execution_item = {
                    "item_id": str(plan_item.item_id),
                    "collector_id": definition.collector_id,
                    "collector_revision": item["snapshot"].revision,
                    "collector_checksum": item["snapshot"].checksum,
                    "executor": item["execution_spec"]["executor"],
                    "timeout_seconds": definition.timeout_seconds,
                    "max_output_bytes": max_bytes,
                    **{key: value for key, value in item["execution_spec"].items() if key != "executor"},
                }
            source_refs = sorted(source_refs_by_collector.get(definition.collector_id, set()))
            if source_refs:
                # 这是签名正文的一部分：离线 Runtime、实验室和审计可以按 KBD/Signal
                # 精确关联执行项，不需要通过易漂移的命令文本进行业务归属推断。
                execution_item["source_signal_refs"] = [
                    {"support_id": support_id, "signal_id": signal_id}
                    for support_id, signal_id in source_refs
                ]
            execution_items.append(execution_item)
        return {
            "schema_version": "1.0",
            "artifact_id": artifact_id,
            "session_id": session_id,
            "collection_plan_id": collection_plan_id,
            "target_key": target_key,
            "execution_items": execution_items,
        }

    @staticmethod
    def _request_hash(command: CollectorArtifactGenerateRequest, target_key: str) -> str:
        payload = {**command.model_dump(mode="json"), "resolved_target_key": target_key}
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _normalize_idempotency_key(idempotency_key: str) -> str:
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
        if actor.roles.isdisjoint(ARTIFACT_ROLES):
            raise DiagnosisError(code="FORBIDDEN", message="当前角色无权操作采集器制品", http_status=403)
