"""离线诊断信号映射、时间线和 Legal Hold（法务保全）治理服务。"""

import json
import secrets
from typing import Any

from shared.observability.otel import get_current_trace_id
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import ActorContext
from app.errors import DiagnosisError
from app.schemas.evidence_lifecycle import LegalHoldRequest, OfflineSignalMappingWrite
from app.services.offline_analysis_service import REPORT_READ_ROLES


class OfflineGovernanceService:
    """维护全局信号映射和租户隔离的诊断治理视图。"""

    def __init__(self, session: AsyncSession):
        self._session = session

    async def list_signal_mappings(self, *, actor: ActorContext) -> list[dict[str, Any]]:
        """按稳定优先级列出 Offline Signal Mapping（离线信号映射）。"""

        self._require_platform_admin(actor)
        result = await self._session.execute(
            text(
                """
                SELECT * FROM offline_signal_collector_mapping
                ORDER BY acquire_tool, category_scope, command_scope, priority, collector_id
                """
            )
        )
        return [dict(row) for row in result.mappings().all()]

    async def analyze_kbd_collection_impact(self, *, actor: ActorContext, kbd_id: int) -> dict[str, Any]:
        """分析 KBD 采集需求对映射、画像、计划和制品的影响，不直接级联修改历史资源。"""

        self._require_platform_admin(actor)
        kbd_result = await self._session.execute(
            text(
                """
                SELECT e.id, e.support_id, e.title, e.category_id, e.status, e.signals_json,
                       e.updated_at, a.active_revision
                FROM kbd_entry e
                LEFT JOIN dynamic_resource_active a
                  ON a.resource_type = 'kbd' AND a.resource_name = e.id::text
                WHERE e.id = :kbd_id
                """
            ),
            {"kbd_id": kbd_id},
        )
        kbd = kbd_result.mappings().one_or_none()
        if kbd is None:
            raise DiagnosisError(code="KBD_NOT_FOUND", message="KBD 不存在", http_status=404)

        document = kbd["signals_json"] or []
        signals = document.get("signals", []) if isinstance(document, dict) else document
        requirements: list[dict[str, str]] = []
        for signal in signals:
            acquire = signal.get("acquire") or {}
            tool = str(acquire.get("tool") or signal.get("acquirer") or "").strip()
            if not tool:
                continue
            args = acquire.get("args") or signal.get("acquirer_args") or {}
            command = str(
                args.get("command")
                or args.get("sub_command")
                or signal.get("sub_command")
                or args.get("resource_keyword")
                or args.get("keyword")
                or "*"
            ).strip()
            requirements.append({"signal_id": str(signal.get("id") or ""), "acquire_tool": tool, "command": command})

        mapping_rows = (
            (
                await self._session.execute(
                    text(
                        """
                    SELECT m.mapping_id, m.acquire_tool, m.category_scope, m.command_scope,
                           m.source_kbd_id, m.source_kbd_revision, m.source_signal_id,
                           m.execution_contract_checksum, m.collector_id, m.query_type, m.priority,
                           c.review_status, c.is_enabled
                    FROM offline_signal_collector_mapping m
                    JOIN collector_definition c ON c.collector_id = m.collector_id
                    WHERE m.is_enabled = true
                    ORDER BY m.priority, m.collector_id
                    """
                    )
                )
            )
            .mappings()
            .all()
        )
        matched: list[dict[str, Any]] = []
        missing: list[dict[str, str]] = []
        collector_ids: set[str] = set()
        for requirement in requirements:
            candidates = [
                row
                for row in mapping_rows
                if row["acquire_tool"] == requirement["acquire_tool"]
                and row["source_kbd_id"] == kbd["id"]
                and row["source_kbd_revision"] == kbd["active_revision"]
                and row["source_signal_id"] == requirement["signal_id"]
                and row["review_status"] == "approved"
                and row["is_enabled"]
            ]
            if not candidates:
                missing.append(requirement)
                continue
            for row in candidates:
                item = {**requirement, **dict(row)}
                matched.append(item)
                collector_ids.add(row["collector_id"])

        profiles: list[dict[str, Any]] = []
        plans: list[dict[str, Any]] = []
        artifacts: list[dict[str, Any]] = []
        if collector_ids:
            params = {"collector_ids": sorted(collector_ids), "tenant_id": actor.tenant_id}
            profile_result = await self._session.execute(
                text(
                    """
                    SELECT DISTINCT p.profile_id, p.review_status, p.is_enabled, p.lock_version
                    FROM collection_profile_definition p,
                         jsonb_array_elements(p.profile_json->'items') item
                    WHERE item->>'collector_id' = ANY(:collector_ids)
                    ORDER BY p.profile_id
                    """
                ),
                params,
            )
            profiles = [dict(row) for row in profile_result.mappings().all()]
            plan_result = await self._session.execute(
                text(
                    """
                    SELECT DISTINCT p.plan_id, p.session_id, p.plan_revision, p.status,
                           p.profile_name, p.created_at
                    FROM collection_plan p
                    JOIN collection_plan_item i ON i.plan_id = p.plan_id
                    WHERE p.tenant_id = :tenant_id
                      AND i.collector_id = ANY(:collector_ids)
                    ORDER BY p.created_at DESC
                    """
                ),
                params,
            )
            plans = [dict(row) for row in plan_result.mappings().all()]
            artifact_result = await self._session.execute(
                text(
                    """
                    SELECT DISTINCT a.artifact_id, a.session_id, a.collection_plan_id,
                           a.file_name, a.status, a.expires_at, a.created_at
                    FROM collector_artifact a
                    JOIN collector_artifact_item i ON i.artifact_id = a.artifact_id
                    WHERE a.tenant_id = :tenant_id
                      AND i.collector_id = ANY(:collector_ids)
                    ORDER BY a.created_at DESC
                    """
                ),
                params,
            )
            artifacts = [dict(row) for row in artifact_result.mappings().all()]

        blockers = [
            {
                "code": "OFFLINE_SIGNAL_MAPPING_MISSING",
                "message": f"{item['acquire_tool']} / {item['command']} 缺少可用离线采集映射",
                "requirement": item,
            }
            for item in missing
        ]
        return {
            "kbd": {
                "kbd_id": kbd["id"],
                "support_id": kbd["support_id"],
                "title": kbd["title"],
                "category_id": kbd["category_id"],
                "status": kbd["status"],
                "updated_at": kbd["updated_at"],
            },
            "change_policy": {
                "content_or_matcher": "仅生成新 KBD 规则快照，不重建采集器制品",
                "evidence_acquisition": "先补映射/画像并审批，再重生成计划；旧计划作废且旧制品自动撤销",
                "archive": "仅停止新计划引用，历史计划和诊断结果继续保留",
            },
            "offline_ready": not blockers,
            "requirements": requirements,
            "matched_mappings": matched,
            "missing_mappings": missing,
            "affected_profiles": profiles,
            "affected_plans": plans,
            "affected_artifacts": artifacts,
            "blockers": blockers,
        }

    async def save_signal_mapping(
        self,
        *,
        actor: ActorContext,
        mapping_id: str,
        command: OfflineSignalMappingWrite,
        if_match: str | None,
    ) -> dict[str, Any]:
        """创建或更新映射；更新必须携带 If-Match（条件版本）。"""

        self._require_platform_admin(actor)
        collector_result = await self._session.execute(
            text(
                """
                SELECT 1 FROM collector_definition
                WHERE collector_id = :collector_id
                  AND review_status = 'approved' AND is_enabled = true
                """
            ),
            {"collector_id": command.collector_id},
        )
        if collector_result.scalar_one_or_none() is None:
            raise DiagnosisError(
                code="OFFLINE_COLLECTOR_NOT_AVAILABLE",
                message="映射引用的 Collector 不存在、未审批或已禁用",
                http_status=422,
            )
        result = await self._session.execute(
            text(
                """
                SELECT * FROM offline_signal_collector_mapping
                WHERE mapping_id = :mapping_id
                FOR UPDATE
                """
            ),
            {"mapping_id": mapping_id},
        )
        existing = result.mappings().one_or_none()
        trace_id = get_current_trace_id() or secrets.token_hex(16)
        values = {
            "mapping_id": mapping_id,
            "source_kbd_id": command.source_kbd_id,
            "source_kbd_revision": command.source_kbd_revision,
            "source_signal_id": command.source_signal_id,
            "execution_contract_checksum": command.execution_contract_checksum,
            "acquire_tool": command.acquire_tool,
            "category_scope": command.category_scope,
            "command_scope": command.command_scope,
            "collector_id": command.collector_id,
            "query_type": command.query_type,
            "field_mapping": json.dumps(command.field_mapping, ensure_ascii=False, sort_keys=True),
            "priority": command.priority,
            "is_enabled": command.is_enabled,
            "trace_id": trace_id,
        }
        if existing is None:
            if if_match is not None:
                raise DiagnosisError(
                    code="SIGNAL_MAPPING_NOT_FOUND",
                    message="离线信号映射不存在，创建时不能提供 If-Match",
                    http_status=404,
                )
            await self._session.execute(
                text(
                    """
                    INSERT INTO offline_signal_collector_mapping (
                        mapping_id, source_kbd_id, source_kbd_revision, source_signal_id,
                        execution_contract_checksum, acquire_tool, category_scope, command_scope, collector_id,
                        query_type, field_mapping, priority, is_enabled, trace_id
                    ) VALUES (
                        :mapping_id, :source_kbd_id, :source_kbd_revision, :source_signal_id,
                        :execution_contract_checksum, :acquire_tool, :category_scope, :command_scope, :collector_id,
                        :query_type, CAST(:field_mapping AS jsonb), :priority, :is_enabled, :trace_id
                    )
                    """
                ),
                values,
            )
        else:
            self._assert_if_match(if_match, existing["lock_version"])
            await self._session.execute(
                text(
                    """
                    UPDATE offline_signal_collector_mapping
                    SET source_kbd_id = :source_kbd_id,
                        source_kbd_revision = :source_kbd_revision,
                        source_signal_id = :source_signal_id,
                        execution_contract_checksum = :execution_contract_checksum,
                        acquire_tool = :acquire_tool, category_scope = :category_scope,
                        command_scope = :command_scope, collector_id = :collector_id,
                        query_type = :query_type, field_mapping = CAST(:field_mapping AS jsonb),
                        priority = :priority, is_enabled = :is_enabled,
                        lock_version = lock_version + 1, trace_id = :trace_id
                    WHERE mapping_id = :mapping_id
                    """
                ),
                values,
            )
        return await self._get_signal_mapping(mapping_id)

    async def get_supplement_plan(self, *, actor: ActorContext, session_id: str) -> dict[str, Any]:
        """读取会话唯一 Supplement Plan（补充采集计划）。"""

        self._require_read_role(actor)
        result = await self._session.execute(
            text(
                """
                SELECT sp.*,
                       cp.plan_id AS collection_plan_id,
                       b.bundle_id AS parent_bundle_id,
                       COALESCE((
                           SELECT jsonb_agg(e.signal_id ORDER BY e.signal_id)
                           FROM signal_evaluation e
                           WHERE e.run_id = sp.run_id AND e.state = 'MATCHED'
                       ), '[]'::jsonb) AS confirmed_findings,
                       COALESCE((
                           SELECT jsonb_agg(e.signal_id ORDER BY e.signal_id)
                           FROM signal_evaluation e
                           WHERE e.run_id = sp.run_id AND e.state = 'UNKNOWN'
                       ), '[]'::jsonb) AS unconfirmed_findings
                FROM supplement_plan sp
                JOIN collection_plan cp
                  ON cp.session_id = sp.session_id
                 AND cp.tenant_id = sp.tenant_id
                 AND cp.plan_sequence = 1
                 AND cp.status = 'ready'
                JOIN LATERAL (
                    SELECT bundle_id
                    FROM diagnostic_evidence_bundle
                    WHERE session_id = sp.session_id
                      AND tenant_id = sp.tenant_id
                      AND bundle_type = 'initial'
                      AND processing_status <> 'deleted'
                    ORDER BY created_at DESC
                    LIMIT 1
                ) b ON true
                WHERE sp.tenant_id = :tenant_id AND sp.session_id = :session_id
                """
            ),
            {"tenant_id": actor.tenant_id, "session_id": session_id},
        )
        row = result.mappings().one_or_none()
        if row is None:
            raise DiagnosisError(code="SUPPLEMENT_PLAN_NOT_FOUND", message="补充采集计划不存在", http_status=404)
        return dict(row)

    async def get_timeline(self, *, actor: ActorContext, session_id: str) -> list[dict[str, Any]]:
        """聚合会话、上传、处理、分析、审核和删除事件。"""

        self._require_read_role(actor)
        result = await self._session.execute(
            text(
                """
                SELECT event_type, event_id, status, occurred_at, trace_id, details
                FROM (
                    SELECT 'session_created'::text AS event_type, s.session_id::text AS event_id,
                           s.status::text AS status, s.created_at AS occurred_at, s.trace_id,
                           jsonb_build_object('case_id', s.case_id, 'scenario', s.selected_scenario) AS details
                    FROM diagnosis_session s
                    WHERE s.tenant_id = :tenant_id AND s.session_id = :session_id
                    UNION ALL
                    SELECT 'upload'::text, u.upload_id::text, u.status, u.created_at, u.trace_id,
                           jsonb_build_object('bundle_type', u.bundle_type, 'part_count', u.part_count)
                    FROM diagnosis_upload_session u
                    WHERE u.tenant_id = :tenant_id AND u.session_id = :session_id
                    UNION ALL
                    SELECT 'bundle'::text, b.bundle_id::text, b.processing_status, b.created_at, b.trace_id,
                           jsonb_build_object('bundle_type', b.bundle_type, 'sha256', b.sha256)
                    FROM diagnostic_evidence_bundle b
                    WHERE b.tenant_id = :tenant_id AND b.session_id = :session_id
                    UNION ALL
                    SELECT 'assessment'::text, a.assessment_id::text,
                           CASE WHEN a.ready_for_diagnosis THEN 'ready' ELSE 'insufficient' END,
                           a.created_at, a.trace_id,
                           jsonb_build_object('completeness_score', a.completeness_score)
                    FROM evidence_assessment a
                    WHERE a.tenant_id = :tenant_id AND a.session_id = :session_id
                    UNION ALL
                    SELECT 'diagnosis_run'::text, r.run_id::text, r.status, r.created_at, r.trace_id,
                           jsonb_build_object('sequence', r.run_sequence, 'resolved_category', r.resolved_category)
                    FROM diagnosis_run r
                    WHERE r.tenant_id = :tenant_id AND r.session_id = :session_id
                    UNION ALL
                    SELECT 'report'::text, p.report_id::text, p.publish_status, p.created_at, p.trace_id,
                           jsonb_build_object('sequence', p.report_sequence, 'diagnosis_level', p.diagnosis_level)
                    FROM diagnosis_report p
                    WHERE p.tenant_id = :tenant_id AND p.session_id = :session_id
                    UNION ALL
                    SELECT 'report_revision'::text, v.revision_id::text, v.action, v.created_at, v.trace_id,
                           jsonb_build_object('report_id', v.report_id, 'actor_id', v.actor_id)
                    FROM diagnosis_report_revision v
                    JOIN diagnosis_report p ON p.report_id = v.report_id
                    WHERE p.tenant_id = :tenant_id AND p.session_id = :session_id
                    UNION ALL
                    SELECT 'legal_hold'::text, h.audit_id::text, h.action, h.created_at, h.trace_id,
                           jsonb_build_object('actor_id', h.actor_id, 'reason', h.reason)
                    FROM diagnosis_legal_hold_audit h
                    WHERE h.tenant_id = :tenant_id AND h.session_id = :session_id
                    UNION ALL
                    SELECT 'deletion'::text, d.deletion_id::text, d.status, d.created_at, d.trace_id,
                           d.deletion_results
                    FROM diagnosis_deletion_job d
                    WHERE d.tenant_id = :tenant_id AND d.session_id = :session_id
                ) events
                ORDER BY occurred_at, event_type, event_id
                """
            ),
            {"tenant_id": actor.tenant_id, "session_id": session_id},
        )
        rows = [dict(row) for row in result.mappings().all()]
        if not rows:
            raise DiagnosisError(code="DIAGNOSIS_SESSION_NOT_FOUND", message="诊断会话不存在", http_status=404)
        return rows

    async def update_legal_hold(
        self,
        *,
        actor: ActorContext,
        session_id: str,
        command: LegalHoldRequest,
    ) -> dict[str, Any]:
        """设置或双人解除会话级法务保全，并同步全部证据包。"""

        self._require_platform_admin(actor)
        result = await self._session.execute(
            text(
                """
                SELECT session_id, legal_hold FROM diagnosis_session
                WHERE tenant_id = :tenant_id AND session_id = :session_id
                FOR UPDATE
                """
            ),
            {"tenant_id": actor.tenant_id, "session_id": session_id},
        )
        session_row = result.mappings().one_or_none()
        if session_row is None:
            raise DiagnosisError(code="DIAGNOSIS_SESSION_NOT_FOUND", message="诊断会话不存在", http_status=404)
        desired = command.action == "apply"
        if bool(session_row["legal_hold"]) == desired:
            return await self.get_legal_hold(actor=actor, session_id=session_id)
        if command.action == "release":
            latest_result = await self._session.execute(
                text(
                    """
                    SELECT actor_id FROM diagnosis_legal_hold_audit
                    WHERE tenant_id = :tenant_id AND session_id = :session_id AND action = 'applied'
                    ORDER BY created_at DESC LIMIT 1
                    """
                ),
                {"tenant_id": actor.tenant_id, "session_id": session_id},
            )
            applied_by = latest_result.scalar_one_or_none()
            if applied_by == actor.user_id:
                raise DiagnosisError(
                    code="LEGAL_HOLD_FOUR_EYES_REQUIRED",
                    message="Legal Hold（法务保全）必须由另一名平台管理员解除",
                    http_status=403,
                )
        bundle_result = await self._session.execute(
            text(
                """
                UPDATE diagnostic_evidence_bundle
                SET legal_hold = :legal_hold, version = version + 1
                WHERE tenant_id = :tenant_id AND session_id = :session_id
                RETURNING bundle_id
                """
            ),
            {"legal_hold": desired, "tenant_id": actor.tenant_id, "session_id": session_id},
        )
        bundle_ids = [str(row[0]) for row in bundle_result.all()]
        await self._session.execute(
            text(
                """
                UPDATE diagnosis_session
                SET legal_hold = :legal_hold, version = version + 1
                WHERE tenant_id = :tenant_id AND session_id = :session_id
                """
            ),
            {"legal_hold": desired, "tenant_id": actor.tenant_id, "session_id": session_id},
        )
        trace_id = get_current_trace_id() or secrets.token_hex(16)
        await self._session.execute(
            text(
                """
                INSERT INTO diagnosis_legal_hold_audit (
                    tenant_id, session_id, action, actor_id, reason, affected_bundle_ids, trace_id
                ) VALUES (
                    :tenant_id, :session_id, :action, :actor_id, :reason,
                    CAST(:bundle_ids AS jsonb), :trace_id
                )
                """
            ),
            {
                "tenant_id": actor.tenant_id,
                "session_id": session_id,
                "action": "applied" if desired else "released",
                "actor_id": actor.user_id,
                "reason": command.reason,
                "bundle_ids": json.dumps(bundle_ids),
                "trace_id": trace_id,
            },
        )
        return await self.get_legal_hold(actor=actor, session_id=session_id)

    async def get_legal_hold(self, *, actor: ActorContext, session_id: str) -> dict[str, Any]:
        """读取会话级法务保全及最近审计事件。"""

        self._require_read_role(actor)
        result = await self._session.execute(
            text(
                """
                SELECT s.session_id, s.legal_hold,
                       COALESCE((
                           SELECT jsonb_agg(b.bundle_id ORDER BY b.created_at)
                           FROM diagnostic_evidence_bundle b
                           WHERE b.session_id = s.session_id AND b.legal_hold = true
                       ), '[]'::jsonb) AS affected_bundle_ids,
                       h.action AS latest_action, h.actor_id AS latest_actor_id,
                       h.reason AS latest_reason, h.created_at AS updated_at
                FROM diagnosis_session s
                LEFT JOIN LATERAL (
                    SELECT action, actor_id, reason, created_at
                    FROM diagnosis_legal_hold_audit
                    WHERE tenant_id = s.tenant_id AND session_id = s.session_id
                    ORDER BY created_at DESC LIMIT 1
                ) h ON true
                WHERE s.tenant_id = :tenant_id AND s.session_id = :session_id
                """
            ),
            {"tenant_id": actor.tenant_id, "session_id": session_id},
        )
        row = result.mappings().one_or_none()
        if row is None:
            raise DiagnosisError(code="DIAGNOSIS_SESSION_NOT_FOUND", message="诊断会话不存在", http_status=404)
        return dict(row)

    async def _get_signal_mapping(self, mapping_id: str) -> dict[str, Any]:
        result = await self._session.execute(
            text("SELECT * FROM offline_signal_collector_mapping WHERE mapping_id = :mapping_id"),
            {"mapping_id": mapping_id},
        )
        row = result.mappings().one_or_none()
        if row is None:
            raise DiagnosisError(code="SIGNAL_MAPPING_NOT_FOUND", message="离线信号映射不存在", http_status=404)
        return dict(row)

    @staticmethod
    def _assert_if_match(value: str | None, current_version: int) -> None:
        if value is None:
            raise DiagnosisError(code="IF_MATCH_REQUIRED", message="更新映射必须提供 If-Match", http_status=428)
        if value.strip().strip('"') != str(current_version):
            raise DiagnosisError(
                code="SIGNAL_MAPPING_VERSION_CONFLICT",
                message="离线信号映射已被其他请求更新",
                http_status=412,
                details={"current_version": current_version},
            )

    @staticmethod
    def _require_platform_admin(actor: ActorContext) -> None:
        if not actor.has_any_role("platform_admin"):
            raise DiagnosisError(code="FORBIDDEN", message="当前角色无权管理离线诊断治理配置", http_status=403)

    @staticmethod
    def _require_read_role(actor: ActorContext) -> None:
        if actor.roles.isdisjoint(REPORT_READ_ROLES):
            raise DiagnosisError(code="FORBIDDEN", message="当前角色无权读取离线诊断治理数据", http_status=403)


def _command_scope_matches(scope: str, command: str) -> bool:
    """匹配命令范围；例如 ps 映射可覆盖 ps auxf。"""

    return scope == "*" or scope == command or command.startswith(f"{scope} ")
