"""诊断会话业务服务。"""

import hashlib
import json
import secrets
import uuid
from dataclasses import dataclass
from typing import Protocol

from shared.observability.logger import get_logger
from shared.observability.otel import get_current_trace_id
from sqlalchemy import text

from app.auth import ActorContext, CaseAuthorizer
from app.domain.session_state import (
    DiagnosisSessionStatus,
    InvalidSessionTransitionError,
    SessionStateMachine,
)
from app.errors import DiagnosisError
from app.models.diagnosis_session import DiagnosisSession
from app.repositories.diagnosis_session_repository import DiagnosisSessionRepository
from app.schemas.collection_profile import CollectionProfileDefinition, profile_requires_affected_object
from app.schemas.diagnosis_session import DiagnosisSessionCreate

logger = get_logger("diagnosis-session-service")

CREATE_ROLES = frozenset(
    {
        "customer_admin",
        "field_engineer",
        "support_engineer",
        "domain_expert",
        "platform_admin",
    }
)
TRANSITION_ROLES = frozenset({"support_engineer", "domain_expert", "platform_admin", "diagnosis_worker"})


class ScenarioAvailability(Protocol):
    """Collection Profile（采集画像）可用性校验协议。"""

    async def assert_scenario_available(self, scenario: str) -> CollectionProfileDefinition | None:
        """确认场景可用，并返回当前生效画像供输入门禁使用。"""


@dataclass(frozen=True, slots=True)
class SessionCreateResult:
    """创建诊断会话的业务结果。"""

    entity: DiagnosisSession
    created: bool


class DiagnosisSessionService:
    """诊断会话应用服务。"""

    def __init__(
        self,
        repository: DiagnosisSessionRepository,
        case_authorizer: CaseAuthorizer | None = None,
        scenario_availability: ScenarioAvailability | None = None,
    ):
        self.repository = repository
        self.case_authorizer = case_authorizer
        self.scenario_availability = scenario_availability

    async def create(
        self,
        *,
        actor: ActorContext,
        command: DiagnosisSessionCreate,
        idempotency_key: str,
    ) -> SessionCreateResult:
        """按租户和幂等键创建诊断会话。"""

        self._require_roles(actor, CREATE_ROLES)
        if self.case_authorizer is None:
            raise DiagnosisError(
                code="CASE_AUTHORIZER_UNAVAILABLE",
                message="工单对象级授权组件尚未配置",
                http_status=503,
                retryable=False,
            )
        await self.case_authorizer.assert_access(actor, command.case_id)
        if self.scenario_availability is None:
            raise DiagnosisError(
                code="COLLECTION_PROFILE_PROVIDER_UNAVAILABLE",
                message="离线诊断场景提供方尚未配置",
                http_status=503,
                retryable=False,
            )
        profile = await self.scenario_availability.assert_scenario_available(command.selected_scenario)
        if (
            profile is not None
            and profile_requires_affected_object(profile)
            and not any((item.id or "").strip() for item in command.affected_objects)
        ):
            raise DiagnosisError(
                code="AFFECTED_OBJECT_REQUIRED",
                message="当前故障场景的采集画像需要故障对象标识",
                http_status=422,
                details={"scenario": command.selected_scenario},
            )

        normalized_key = idempotency_key.strip()
        if not normalized_key or len(normalized_key) > 128:
            raise DiagnosisError(
                code="INVALID_IDEMPOTENCY_KEY",
                message="Idempotency-Key 不能为空且长度不能超过 128",
                http_status=422,
            )

        request_hash = self._request_hash(command)
        trace_id = get_current_trace_id() or secrets.token_hex(16)
        values = {
            "session_id": uuid.uuid4(),
            "case_id": command.case_id,
            "tenant_id": actor.tenant_id,
            "created_by": actor.user_id,
            "product_line": command.product_line,
            "selected_scenario": command.selected_scenario,
            "selected_category": command.selected_category,
            "incident_start_time": command.incident.start_time,
            "incident_end_time": command.incident.end_time,
            "incident_timezone": command.incident.timezone,
            "affected_objects": [item.model_dump(mode="json") for item in command.affected_objects],
            "impact_scope": command.impact_scope,
            "incident_status": command.current_status,
            "recent_change_description": command.recent_change_description,
            "experimental": command.experimental,
            "status": DiagnosisSessionStatus.CREATED,
            "supplement_count": 0,
            "version": 1,
            "idempotency_key": normalized_key,
            "request_hash": request_hash,
            "trace_id": trace_id,
        }

        result = await self.repository.create_idempotent(values)
        if not result.created and result.entity.request_hash != request_hash:
            raise DiagnosisError(
                code="IDEMPOTENCY_KEY_REUSED",
                message="同一 Idempotency-Key 已用于不同请求",
                http_status=409,
                details={"session_id": str(result.entity.session_id)},
            )

        logger.info(
            event="diagnosis_session_created" if result.created else "diagnosis_session_replayed",
            session_id=str(result.entity.session_id),
            tenant_id=actor.tenant_id,
            case_id=command.case_id,
            trace_id=trace_id,
        )
        return SessionCreateResult(entity=result.entity, created=result.created)

    async def get(self, *, actor: ActorContext, session_id: str):
        """读取本租户诊断会话。"""

        entity = await self.repository.get_by_id_for_tenant(session_id, actor.tenant_id)
        if entity is None:
            raise DiagnosisError(
                code="DIAGNOSIS_SESSION_NOT_FOUND",
                message="诊断会话不存在",
                http_status=404,
            )
        return entity

    async def resume_workspace(self, *, actor: ActorContext, case_id: str) -> dict:
        """恢复工单最近一次离线诊断的非敏感资源标识。"""

        self._require_roles(actor, CREATE_ROLES)
        if self.case_authorizer is None:
            raise DiagnosisError(code="CASE_AUTHORIZER_UNAVAILABLE", message="工单授权组件不可用", http_status=503)
        await self.case_authorizer.assert_access(actor, case_id)
        entity = await self.repository.get_latest_by_case(case_id, actor.tenant_id)
        if entity is None:
            raise DiagnosisError(code="DIAGNOSIS_SESSION_NOT_FOUND", message="该工单没有可恢复的离线诊断", http_status=404)
        result = await self.repository.session.execute(
            text(
                """
                SELECT
                    (SELECT plan_id FROM collection_plan
                     WHERE session_id = :session_id AND status = 'ready'
                     ORDER BY plan_sequence DESC, plan_revision DESC LIMIT 1) AS plan_id,
                    (SELECT artifact_id FROM collector_artifact
                     WHERE session_id = :session_id AND status = 'ready'
                     ORDER BY created_at DESC LIMIT 1) AS artifact_id,
                    (SELECT upload_id FROM diagnosis_upload_session
                     WHERE session_id = :session_id AND status IN ('initiated', 'uploading', 'completing')
                     ORDER BY created_at DESC LIMIT 1) AS active_upload_id
                """
            ),
            {"session_id": entity.session_id},
        )
        resources = result.mappings().one()
        return {
            "session": entity,
            "plan_id": resources["plan_id"],
            "artifact_id": resources["artifact_id"],
            "active_upload_id": resources["active_upload_id"],
        }

    async def transition(
        self,
        *,
        actor: ActorContext,
        session_id: str,
        target: DiagnosisSessionStatus,
        failure_code: str | None = None,
        failure_message: str | None = None,
    ):
        """在行锁保护下转换会话状态。"""

        self._require_roles(actor, TRANSITION_ROLES)
        entity = await self.repository.get_by_id_for_update(session_id, actor.tenant_id)
        if entity is None:
            raise DiagnosisError(
                code="DIAGNOSIS_SESSION_NOT_FOUND",
                message="诊断会话不存在",
                http_status=404,
            )

        source = DiagnosisSessionStatus(entity.status)
        resume_status = DiagnosisSessionStatus(entity.resume_status) if entity.resume_status else None
        try:
            SessionStateMachine.validate(source, target, resume_status=resume_status)
        except InvalidSessionTransitionError as exc:
            raise DiagnosisError(
                code="INVALID_SESSION_TRANSITION",
                message="诊断会话状态转换不合法",
                http_status=409,
                details={"source": source.value, "target": target.value},
            ) from exc

        if source == target:
            return entity

        if target == DiagnosisSessionStatus.SUPPLEMENT_REQUIRED:
            if entity.supplement_count >= 1:
                raise DiagnosisError(
                    code="SUPPLEMENT_LIMIT_REACHED",
                    message="P0 只允许一次自动补充采集",
                    http_status=409,
                )
            entity.supplement_count += 1

        if target == DiagnosisSessionStatus.FAILED:
            entity.resume_status = source
        elif source == DiagnosisSessionStatus.FAILED:
            entity.resume_status = None

        entity.status = target
        entity.version += 1
        entity.failure_code = failure_code if target == DiagnosisSessionStatus.FAILED else None
        entity.failure_message = failure_message if target == DiagnosisSessionStatus.FAILED else None
        updated = await self.repository.flush(entity)

        logger.info(
            event="diagnosis_session_transitioned",
            session_id=str(entity.session_id),
            tenant_id=actor.tenant_id,
            source=source.value,
            target=target.value,
            version=entity.version,
        )
        return updated

    @staticmethod
    def _request_hash(command: DiagnosisSessionCreate) -> str:
        """计算幂等请求指纹。"""

        canonical = json.dumps(
            command.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _require_roles(actor: ActorContext, allowed_roles: frozenset[str]) -> None:
        """校验业务角色。"""

        if actor.roles.isdisjoint(allowed_roles):
            raise DiagnosisError(
                code="FORBIDDEN",
                message="当前角色无权执行此操作",
                http_status=403,
            )
