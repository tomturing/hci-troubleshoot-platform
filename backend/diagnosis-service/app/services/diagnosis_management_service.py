"""离线诊断管理工作台聚合与处置服务。"""

import json
import secrets
import uuid
from typing import Any

from shared.observability.otel import get_current_trace_id
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import ActorContext
from app.errors import DiagnosisError


class DiagnosisManagementService:
    """提供租户隔离的跨会话运营视图和受控管理动作。"""

    def __init__(self, session: AsyncSession):
        self._session = session

    async def list_sessions(
        self,
        *,
        actor: ActorContext,
        query: str | None,
        status: str | None,
        assigned_to: str | None,
        offset: int,
        limit: int,
    ) -> dict[str, Any]:
        """按会话、工单或客户检索诊断会话。"""

        self._require_operator(actor)
        filters = ["s.tenant_id = :tenant_id"]
        params: dict[str, Any] = {"tenant_id": actor.tenant_id, "offset": offset, "limit": limit}
        if query:
            filters.append(
                "(s.session_id::text ILIKE :query OR s.case_id ILIKE :query OR COALESCE(c.customer_id::text, '') ILIKE :query)"
            )
            params["query"] = f"%{query.strip()}%"
        if status:
            filters.append("s.status::text = :status")
            params["status"] = status
        if assigned_to:
            filters.append("s.assigned_to = :assigned_to")
            params["assigned_to"] = assigned_to
        where = " AND ".join(filters)
        count_result = await self._session.execute(
            text(f'SELECT COUNT(*) FROM diagnosis_session s JOIN "case" c ON c.case_id = s.case_id WHERE {where}'),
            params,
        )
        result = await self._session.execute(
            text(
                f"""
                SELECT s.session_id, s.case_id, c.customer_id::text AS customer_id,
                       s.selected_scenario, s.status::text AS status, s.assigned_to,
                       s.supplement_count, s.trace_id, s.created_at, s.updated_at,
                       report.publish_status AS latest_report_status,
                       report.report_sequence AS latest_report_sequence,
                       (SELECT COUNT(*) FROM diagnostic_evidence_bundle b
                        WHERE b.session_id = s.session_id AND b.processing_status <> 'deleted') AS bundle_count,
                       (SELECT COUNT(*) FROM diagnosis_processing_job j
                        WHERE j.session_id = s.session_id AND j.status = 'failed') AS failed_task_count
                FROM diagnosis_session s
                JOIN "case" c ON c.case_id = s.case_id
                LEFT JOIN LATERAL (
                    SELECT publish_status, report_sequence
                    FROM diagnosis_report
                    WHERE session_id = s.session_id
                    ORDER BY report_sequence DESC
                    LIMIT 1
                ) report ON true
                WHERE {where}
                ORDER BY s.updated_at DESC, s.session_id DESC
                OFFSET :offset LIMIT :limit
                """
            ),
            params,
        )
        response = {
            "items": [dict(row) for row in result.mappings().all()],
            "total": count_result.scalar_one(),
            "offset": offset,
            "limit": limit,
        }
        await self._record_access(
            actor=actor,
            action="list_sessions",
            resource_type="diagnosis_session",
            details={
                "query": query,
                "status": status,
                "assigned_to": assigned_to,
                "result_count": len(response["items"]),
            },
        )
        return response

    async def assign(self, *, actor: ActorContext, session_id: str, assigned_to: str) -> dict[str, Any]:
        """转派诊断会话并保留更新时间和链路标识。"""

        self._require_operator(actor)
        trace_id = self._trace_id()
        result = await self._session.execute(
            text(
                """
                UPDATE diagnosis_session
                SET assigned_to = :assigned_to, version = version + 1,
                    trace_id = :trace_id, updated_at = CURRENT_TIMESTAMP
                WHERE tenant_id = :tenant_id AND session_id = :session_id
                RETURNING session_id, status::text AS status
                """
            ),
            {
                "assigned_to": assigned_to,
                "trace_id": trace_id,
                "tenant_id": actor.tenant_id,
                "session_id": session_id,
            },
        )
        row = result.mappings().one_or_none()
        if row is None:
            self._not_found()
        await self._record_access(
            actor=actor,
            action="assign",
            resource_type="diagnosis_session",
            resource_id=session_id,
            session_id=session_id,
            details={"assigned_to": assigned_to},
            trace_id=trace_id,
        )
        return {
            "resource_id": str(row["session_id"]),
            "status": row["status"],
            "trace_id": trace_id,
            "details": {"assigned_to": assigned_to},
        }

    async def terminate(self, *, actor: ActorContext, session_id: str, reason: str) -> dict[str, Any]:
        """终止非终态会话，同时停止未完成上传和待执行任务。"""

        self._require_operator(actor)
        trace_id = self._trace_id()
        result = await self._session.execute(
            text(
                """
                UPDATE diagnosis_session
                SET status = 'cancelled', failure_code = 'ADMIN_TERMINATED',
                    failure_message = :reason, version = version + 1,
                    trace_id = :trace_id, updated_at = CURRENT_TIMESTAMP
                WHERE tenant_id = :tenant_id AND session_id = :session_id
                  AND status NOT IN ('closed', 'cancelled', 'deleted')
                RETURNING session_id
                """
            ),
            {
                "reason": reason,
                "trace_id": trace_id,
                "tenant_id": actor.tenant_id,
                "session_id": session_id,
            },
        )
        row = result.mappings().one_or_none()
        if row is None:
            self._not_found("诊断会话不存在或已进入终态")
        await self._session.execute(
            text(
                """
                UPDATE diagnosis_upload_session SET status = 'aborted', updated_at = CURRENT_TIMESTAMP
                WHERE session_id = :session_id AND status IN ('initiated', 'uploading')
                """
            ),
            {"session_id": session_id},
        )
        await self._session.execute(
            text(
                """
                UPDATE supplement_plan SET status = 'cancelled', updated_at = CURRENT_TIMESTAMP
                WHERE session_id = :session_id AND status IN ('ready', 'collecting')
                """
            ),
            {"session_id": session_id},
        )
        await self._session.execute(
            text(
                """
                UPDATE diagnosis_processing_job
                SET status = 'failed', failure_code = 'ADMIN_TERMINATED',
                    failure_message = :reason, trace_id = :trace_id,
                    updated_at = CURRENT_TIMESTAMP
                WHERE session_id = :session_id AND status = 'pending'
                """
            ),
            {"session_id": session_id, "reason": reason, "trace_id": trace_id},
        )
        await self._record_access(
            actor=actor,
            action="terminate",
            resource_type="diagnosis_session",
            resource_id=session_id,
            session_id=session_id,
            details={"reason": reason},
            trace_id=trace_id,
        )
        return {
            "resource_id": session_id,
            "status": "cancelled",
            "trace_id": trace_id,
            "details": {"reason": reason},
        }

    async def retry_processing(self, *, actor: ActorContext, session_id: str) -> dict[str, Any]:
        """重置最近一次可重试失败任务；安全拒绝事件必须先处置，不能直接重试。"""

        self._require_operator(actor)
        trace_id = self._trace_id()
        result = await self._session.execute(
            text(
                """
                WITH candidate AS (
                    SELECT j.task_id, j.bundle_id
                    FROM diagnosis_processing_job j
                    JOIN diagnostic_evidence_bundle b ON b.bundle_id = j.bundle_id
                    WHERE j.tenant_id = :tenant_id AND j.session_id = :session_id
                      AND j.status = 'failed' AND j.attempts < j.max_attempts
                      AND b.processing_status = 'failed'
                    ORDER BY j.updated_at DESC
                    LIMIT 1
                    FOR UPDATE OF j
                )
                UPDATE diagnosis_processing_job j
                SET status = 'pending', available_at = CURRENT_TIMESTAMP,
                    locked_by = NULL, locked_at = NULL,
                    failure_code = NULL, failure_message = NULL,
                    trace_id = :trace_id, updated_at = CURRENT_TIMESTAMP
                FROM candidate c
                WHERE j.task_id = c.task_id
                RETURNING j.task_id, j.bundle_id
                """
            ),
            {"tenant_id": actor.tenant_id, "session_id": session_id, "trace_id": trace_id},
        )
        row = result.mappings().one_or_none()
        if row is None:
            raise DiagnosisError(
                code="NO_RETRYABLE_PROCESSING_JOB",
                message="没有可重试的后台处理任务；安全拒绝事件需先在隔离区治理页处置",
                http_status=409,
            )
        await self._session.execute(
            text(
                """
                UPDATE diagnostic_evidence_bundle
                SET processing_status = 'uploaded', failure_code = NULL, failure_message = NULL,
                    version = version + 1, updated_at = CURRENT_TIMESTAMP
                WHERE bundle_id = :bundle_id
                """
            ),
            {"bundle_id": row["bundle_id"]},
        )
        await self._record_access(
            actor=actor,
            action="retry_processing",
            resource_type="diagnosis_processing_job",
            resource_id=str(row["task_id"]),
            session_id=session_id,
            details={"bundle_id": str(row["bundle_id"])},
            trace_id=trace_id,
        )
        return {
            "resource_id": str(row["task_id"]),
            "status": "pending",
            "trace_id": trace_id,
            "details": {"bundle_id": str(row["bundle_id"])},
        }

    async def list_report_reviews(self, *, actor: ActorContext) -> list[dict[str, Any]]:
        """列出当前租户待审核、待发布和被驳回的报告。"""

        self._require_operator(actor)
        result = await self._session.execute(
            text(
                """
                SELECT 'report_review'::text AS record_type, r.report_id::text AS resource_id,
                       r.session_id, r.publish_status AS status, r.updated_at AS occurred_at,
                       r.trace_id,
                       jsonb_build_object(
                         'case_id', s.case_id, 'report_sequence', r.report_sequence,
                         'diagnosis_level', r.diagnosis_level, 'summary', r.summary,
                         'version', r.version, 'assigned_to', s.assigned_to
                       ) AS details
                FROM diagnosis_report r
                JOIN diagnosis_session s ON s.session_id = r.session_id
                WHERE r.tenant_id = :tenant_id
                  AND r.publish_status IN ('draft', 'review_pending', 'engineer_confirmed', 'rejected')
                ORDER BY r.updated_at DESC
                """
            ),
            {"tenant_id": actor.tenant_id},
        )
        rows = [dict(row) for row in result.mappings().all()]
        await self._record_access(
            actor=actor,
            action="list_report_reviews",
            resource_type="diagnosis_report",
            details={"result_count": len(rows)},
        )
        return rows

    async def list_security_events(self, *, actor: ActorContext) -> list[dict[str, Any]]:
        """只暴露拒绝包的安全元数据，不返回证据正文和对象存储位置。"""

        self._require_operator(actor)
        result = await self._session.execute(
            text(
                """
                SELECT 'security_event'::text AS record_type, b.bundle_id::text AS resource_id,
                       b.session_id, b.security_review_status AS status, b.updated_at AS occurred_at,
                       b.trace_id,
                       jsonb_build_object(
                         'bundle_type', b.bundle_type, 'failure_code', b.failure_code,
                         'failure_message', b.failure_message, 'size_bytes', b.size_bytes,
                         'sha256', b.sha256, 'security_results', b.security_results,
                         'reviewed_by', b.security_reviewed_by,
                         'reviewed_at', b.security_reviewed_at,
                         'review_note', b.security_review_note
                       ) AS details
                FROM diagnostic_evidence_bundle b
                WHERE b.tenant_id = :tenant_id AND b.processing_status = 'rejected'
                ORDER BY b.updated_at DESC
                """
            ),
            {"tenant_id": actor.tenant_id},
        )
        rows = [dict(row) for row in result.mappings().all()]
        await self._record_access(
            actor=actor,
            action="list_security_events",
            resource_type="diagnostic_evidence_bundle",
            details={"result_count": len(rows)},
        )
        return rows

    async def review_security_event(
        self,
        *,
        actor: ActorContext,
        bundle_id: str,
        action: str,
        note: str,
    ) -> dict[str, Any]:
        """确认或清除隔离区安全事件，拒绝包本身仍保留并继续禁止诊断。"""

        self._require_operator(actor)
        target = "acknowledged" if action == "acknowledge" else "cleared"
        trace_id = self._trace_id()
        result = await self._session.execute(
            text(
                """
                UPDATE diagnostic_evidence_bundle
                SET security_review_status = :target, security_reviewed_by = :actor_id,
                    security_reviewed_at = CURRENT_TIMESTAMP, security_review_note = :note,
                    version = version + 1, trace_id = :trace_id, updated_at = CURRENT_TIMESTAMP
                WHERE tenant_id = :tenant_id AND bundle_id = :bundle_id
                  AND processing_status = 'rejected'
                RETURNING bundle_id, session_id
                """
            ),
            {
                "target": target,
                "actor_id": actor.user_id,
                "note": note,
                "trace_id": trace_id,
                "tenant_id": actor.tenant_id,
                "bundle_id": bundle_id,
            },
        )
        reviewed = result.mappings().one_or_none()
        if reviewed is None:
            self._not_found("安全事件不存在")
        await self._record_access(
            actor=actor,
            action=f"security_{action}",
            resource_type="diagnostic_evidence_bundle",
            resource_id=bundle_id,
            session_id=str(reviewed["session_id"]),
            details={"note": note},
            trace_id=trace_id,
        )
        return {
            "resource_id": bundle_id,
            "status": target,
            "trace_id": trace_id,
            "details": {"reviewed_by": actor.user_id, "note": note},
        }

    async def list_governance(self, *, actor: ActorContext) -> list[dict[str, Any]]:
        """聚合法务保全和删除任务状态。"""

        self._require_operator(actor)
        result = await self._session.execute(
            text(
                """
                SELECT record_type, resource_id, session_id, status, occurred_at, trace_id, details
                FROM (
                    SELECT 'legal_hold'::text AS record_type, h.audit_id::text AS resource_id,
                           h.session_id, h.action AS status, h.created_at AS occurred_at, h.trace_id,
                           jsonb_build_object('actor_id', h.actor_id, 'reason', h.reason) AS details
                    FROM diagnosis_legal_hold_audit h
                    WHERE h.tenant_id = :tenant_id
                    UNION ALL
                    SELECT 'deletion_job'::text, d.deletion_id::text, d.session_id,
                           d.status, d.updated_at, d.trace_id,
                           jsonb_build_object(
                             'requested_by', d.requested_by, 'attempts', d.attempts,
                             'results', d.deletion_results, 'failure_message', d.failure_message
                           )
                    FROM diagnosis_deletion_job d
                    WHERE d.tenant_id = :tenant_id
                ) records
                ORDER BY occurred_at DESC
                """
            ),
            {"tenant_id": actor.tenant_id},
        )
        rows = [dict(row) for row in result.mappings().all()]
        await self._record_access(
            actor=actor,
            action="list_governance",
            resource_type="diagnosis_governance",
            details={"result_count": len(rows)},
        )
        return rows

    async def list_audit(self, *, actor: ActorContext, limit: int) -> list[dict[str, Any]]:
        """返回全局访问与业务动作审计时间线。"""

        self._require_operator(actor)
        result = await self._session.execute(
            text(
                """
                SELECT record_type, resource_id, session_id, status, occurred_at, trace_id, details
                FROM (
                    SELECT 'session'::text AS record_type, s.session_id::text AS resource_id,
                           s.session_id, s.status::text AS status, s.updated_at AS occurred_at, s.trace_id,
                           jsonb_build_object(
                             'case_id', s.case_id, 'assigned_to', s.assigned_to,
                             'failure_code', s.failure_code, 'failure_message', s.failure_message
                           ) AS details
                    FROM diagnosis_session s WHERE s.tenant_id = :tenant_id
                    UNION ALL
                    SELECT 'report_revision'::text, v.revision_id::text, r.session_id,
                           v.action, v.created_at, v.trace_id,
                           jsonb_build_object('report_id', v.report_id, 'actor_id', v.actor_id, 'reason', v.reason)
                    FROM diagnosis_report_revision v
                    JOIN diagnosis_report r ON r.report_id = v.report_id
                    WHERE r.tenant_id = :tenant_id
                    UNION ALL
                    SELECT 'legal_hold'::text, h.audit_id::text, h.session_id,
                           h.action, h.created_at, h.trace_id,
                           jsonb_build_object('actor_id', h.actor_id, 'reason', h.reason)
                    FROM diagnosis_legal_hold_audit h WHERE h.tenant_id = :tenant_id
                    UNION ALL
                    SELECT 'management_access'::text, a.audit_id::text, a.session_id,
                           a.action, a.created_at, a.trace_id,
                           jsonb_build_object(
                             'actor_id', a.actor_id, 'actor_roles', a.actor_roles,
                             'resource_type', a.resource_type, 'resource_id', a.resource_id,
                             'result', a.result, 'context', a.details
                           )
                    FROM diagnosis_management_audit a WHERE a.tenant_id = :tenant_id
                ) audit
                ORDER BY occurred_at DESC
                LIMIT :limit
                """
            ),
            {"tenant_id": actor.tenant_id, "limit": limit},
        )
        rows = [dict(row) for row in result.mappings().all()]
        await self._record_access(
            actor=actor,
            action="list_global_audit",
            resource_type="diagnosis_management_audit",
            details={"result_count": len(rows), "limit": limit},
        )
        return rows

    async def _record_access(
        self,
        *,
        actor: ActorContext,
        action: str,
        resource_type: str,
        resource_id: str | None = None,
        session_id: str | None = None,
        details: dict[str, Any] | None = None,
        trace_id: str | None = None,
    ) -> None:
        """写入不含证据正文和凭据的管理访问审计。"""

        await self._session.execute(
            text(
                """
                INSERT INTO diagnosis_management_audit (
                    audit_id, tenant_id, actor_id, actor_roles, action,
                    resource_type, resource_id, session_id, result, details, trace_id
                ) VALUES (
                    :audit_id, :tenant_id, :actor_id, CAST(:actor_roles AS jsonb), :action,
                    :resource_type, :resource_id, :session_id, 'success',
                    CAST(:details AS jsonb), :trace_id
                )
                """
            ),
            {
                "audit_id": uuid.uuid4(),
                "tenant_id": actor.tenant_id,
                "actor_id": actor.user_id,
                "actor_roles": json.dumps(sorted(actor.roles)),
                "action": action,
                "resource_type": resource_type,
                "resource_id": resource_id,
                "session_id": session_id,
                "details": json.dumps(details or {}, ensure_ascii=False),
                "trace_id": trace_id or self._trace_id(),
            },
        )

    @staticmethod
    def _trace_id() -> str:
        return get_current_trace_id() or secrets.token_hex(16)

    @staticmethod
    def _require_operator(actor: ActorContext) -> None:
        if not actor.has_any_role("support_engineer", "domain_expert", "platform_admin", "diagnosis_worker"):
            raise DiagnosisError(code="FORBIDDEN", message="当前角色无权访问离线诊断管理工作台", http_status=403)

    @staticmethod
    def _not_found(message: str = "诊断会话不存在") -> None:
        raise DiagnosisError(code="DIAGNOSIS_RESOURCE_NOT_FOUND", message=message, http_status=404)
