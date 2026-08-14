"""诊断数据留存、Legal Hold（法务保全）和异步删除。"""

import json
import secrets
import uuid
from typing import Any

from shared.observability.otel import get_current_trace_id
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import ActorContext
from app.errors import DiagnosisError
from app.services.object_storage import LocalObjectStorage


class DiagnosisDeletionService:
    """发起并执行诊断数据删除任务。"""

    def __init__(self, *, session: AsyncSession, storage: LocalObjectStorage):
        self._session = session
        self._storage = storage

    async def request(self, *, actor: ActorContext, session_id: str, reason: str) -> dict[str, Any]:
        """幂等创建删除任务；任一 Bundle 有 Legal Hold 时默认拒绝。"""

        if not actor.has_any_role("customer_admin", "platform_admin"):
            raise DiagnosisError(code="FORBIDDEN", message="当前角色无权删除诊断数据", http_status=403)
        result = await self._session.execute(
            text(
                """
                SELECT s.session_id, s.status,
                       (s.legal_hold OR EXISTS (
                           SELECT 1 FROM diagnostic_evidence_bundle b
                           WHERE b.session_id = s.session_id AND b.legal_hold = true
                       )) AS has_legal_hold
                FROM diagnosis_session s
                WHERE s.session_id = :session_id AND s.tenant_id = :tenant_id
                FOR UPDATE
                """
            ),
            {"session_id": session_id, "tenant_id": actor.tenant_id},
        )
        session_row = result.mappings().one_or_none()
        if session_row is None:
            raise DiagnosisError(code="DIAGNOSIS_SESSION_NOT_FOUND", message="诊断会话不存在", http_status=404)
        if session_row["has_legal_hold"]:
            raise DiagnosisError(
                code="LEGAL_HOLD_ACTIVE",
                message="诊断证据处于 Legal Hold（法务保全），解除审批前禁止删除",
                http_status=423,
            )
        existing_result = await self._session.execute(
            text("SELECT * FROM diagnosis_deletion_job WHERE session_id = :session_id"),
            {"session_id": session_id},
        )
        existing = existing_result.mappings().one_or_none()
        if existing:
            return dict(existing)
        deletion_id = uuid.uuid4()
        trace_id = get_current_trace_id() or secrets.token_hex(16)
        await self._session.execute(
            text(
                """
                INSERT INTO diagnosis_deletion_job (
                    deletion_id, tenant_id, session_id, requested_by,
                    deletion_results, failure_message, trace_id
                ) VALUES (
                    :deletion_id, :tenant_id, :session_id, :requested_by,
                    CAST(:results AS jsonb), :reason, :trace_id
                )
                """
            ),
            {
                "deletion_id": deletion_id,
                "tenant_id": actor.tenant_id,
                "session_id": session_id,
                "requested_by": actor.user_id,
                "results": json.dumps({"request_reason": reason}),
                "reason": None,
                "trace_id": trace_id,
            },
        )
        await self._session.execute(
            text(
                """
                UPDATE diagnosis_session
                SET status = 'deletion_pending', version = version + 1, updated_at = CURRENT_TIMESTAMP
                WHERE session_id = :session_id
                """
            ),
            {"session_id": session_id},
        )
        return await self.get(actor=actor, session_id=session_id)

    async def execute(self, *, actor: ActorContext, session_id: str) -> dict[str, Any]:
        """Worker/管理员执行删除；报告和审核审计按合规策略保留。"""

        if not actor.has_any_role("diagnosis_worker", "platform_admin"):
            raise DiagnosisError(code="FORBIDDEN", message="当前角色无权执行诊断数据删除", http_status=403)
        result = await self._session.execute(
            text(
                """
                SELECT j.*, (s.legal_hold OR EXISTS (
                    SELECT 1 FROM diagnostic_evidence_bundle b
                    WHERE b.session_id = j.session_id AND b.legal_hold = true
                )) AS has_legal_hold
                FROM diagnosis_deletion_job j
                JOIN diagnosis_session s ON s.session_id = j.session_id
                WHERE j.session_id = :session_id AND j.tenant_id = :tenant_id
                FOR UPDATE
                """
            ),
            {"session_id": session_id, "tenant_id": actor.tenant_id},
        )
        job = result.mappings().one_or_none()
        if job is None:
            raise DiagnosisError(code="DELETION_JOB_NOT_FOUND", message="删除任务不存在", http_status=404)
        if job["status"] == "deleted":
            return dict(job)
        if job["has_legal_hold"]:
            raise DiagnosisError(code="LEGAL_HOLD_ACTIVE", message="法务保全阻止删除", http_status=423)
        bundle_result = await self._session.execute(
            text(
                """
                SELECT bundle_id, object_key FROM diagnostic_evidence_bundle
                WHERE session_id = :session_id AND tenant_id = :tenant_id
                FOR UPDATE
                """
            ),
            {"session_id": session_id, "tenant_id": actor.tenant_id},
        )
        bundles = [dict(row) for row in bundle_result.mappings().all()]
        deleted_objects: list[str] = []
        try:
            for bundle in bundles:
                await self._storage.delete_object(bundle["object_key"])
                deleted_objects.append(str(bundle["bundle_id"]))
            evidence_result = await self._session.execute(
                text("DELETE FROM evidence_item WHERE session_id = :session_id RETURNING evidence_id"),
                {"session_id": session_id},
            )
            evidence_count = len(evidence_result.all())
            await self._session.execute(
                text(
                    """
                    UPDATE diagnostic_evidence_bundle
                    SET processing_status = 'deleted', deleted_at = CURRENT_TIMESTAMP,
                        manifest_json = NULL, security_results = '{}'::jsonb,
                        encryption_metadata = '{}'::jsonb, version = version + 1,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE session_id = :session_id
                    """
                ),
                {"session_id": session_id},
            )
            results = {
                "object_storage": {"status": "deleted", "bundle_ids": deleted_objects},
                "structured_evidence": {"status": "deleted", "count": evidence_count},
                "temporary_directories": {"status": "lifecycle_managed"},
                "presigned_urls": {"status": "expired"},
                "reports": {"status": "retained_for_audit"},
            }
            await self._session.execute(
                text(
                    """
                    UPDATE diagnosis_deletion_job
                    SET status = 'deleted', attempts = attempts + 1,
                        deletion_results = CAST(:results AS jsonb), failure_message = NULL,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE deletion_id = :deletion_id
                    """
                ),
                {"deletion_id": job["deletion_id"], "results": json.dumps(results)},
            )
            await self._session.execute(
                text(
                    """
                    UPDATE diagnosis_session
                    SET status = 'deleted', version = version + 1, updated_at = CURRENT_TIMESTAMP
                    WHERE session_id = :session_id
                    """
                ),
                {"session_id": session_id},
            )
        except Exception as exc:
            await self._session.execute(
                text(
                    """
                    UPDATE diagnosis_deletion_job
                    SET status = 'deletion_failed', attempts = attempts + 1,
                        failure_message = :message, updated_at = CURRENT_TIMESTAMP
                    WHERE deletion_id = :deletion_id
                    """
                ),
                {"deletion_id": job["deletion_id"], "message": type(exc).__name__},
            )
            raise
        return await self.get(actor=actor, session_id=session_id)

    async def get(self, *, actor: ActorContext, session_id: str) -> dict[str, Any]:
        """读取本租户删除状态。"""

        if not actor.has_any_role(
            "customer_admin", "support_engineer", "domain_expert", "platform_admin", "diagnosis_worker"
        ):
            raise DiagnosisError(code="FORBIDDEN", message="当前角色无权读取删除状态", http_status=403)
        result = await self._session.execute(
            text(
                """
                SELECT * FROM diagnosis_deletion_job
                WHERE session_id = :session_id AND tenant_id = :tenant_id
                """
            ),
            {"session_id": session_id, "tenant_id": actor.tenant_id},
        )
        row = result.mappings().one_or_none()
        if row is None:
            raise DiagnosisError(code="DELETION_JOB_NOT_FOUND", message="删除任务不存在", http_status=404)
        return dict(row)
