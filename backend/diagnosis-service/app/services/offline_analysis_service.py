"""离线证据查询、Signal 评估、KBD 候选、报告和补采编排。"""

import hashlib
import json
import math
import re
import secrets
import uuid
from collections import defaultdict
from typing import Any

from shared.observability.otel import get_current_trace_id
from shared.observability.redaction import redact_observation_value
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import ActorContext
from app.config import settings
from app.errors import DiagnosisError
from app.schemas.evidence_lifecycle import EvidenceQueryRequest, ReportReviewRequest

ASSESSMENT_ALGORITHM_VERSION = "completeness-v3"
MATCHER_VERSION = "offline-matcher-v1"
AGENT_VERSION = "offline-agent-v1"
CONCLUSION_POLICY_VERSION = "conclusion-policy-v1"
REPORT_SCHEMA_VERSION = "diagnosis-report-v1"
ANALYSIS_ROLES = frozenset({"support_engineer", "domain_expert", "platform_admin", "diagnosis_worker"})
REPORT_READ_ROLES = frozenset(
    {"customer_admin", "field_engineer", "support_engineer", "domain_expert", "platform_admin", "diagnosis_worker"}
)
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


class OfflineEvidenceProvider:
    """只查询本会话不可变 Evidence Item（证据项）的受控 Provider（查询器）。"""

    def __init__(self, session: AsyncSession):
        self._session = session

    async def query(self, *, actor: ActorContext, session_id: str, command: EvidenceQueryRequest) -> dict[str, Any]:
        """执行有界元数据/结构化内容查询。"""

        if actor.roles.isdisjoint(REPORT_READ_ROLES):
            raise DiagnosisError(code="FORBIDDEN", message="当前角色无权查询离线证据", http_status=403)
        conditions = ["tenant_id = :tenant_id", "session_id = :session_id"]
        params: dict[str, Any] = {
            "tenant_id": actor.tenant_id,
            "session_id": session_id,
            "limit": command.limit,
        }
        if command.collector_id:
            conditions.append("collector_id = :collector_id")
            params["collector_id"] = command.collector_id
        if command.source_path:
            conditions.append("source_path = :source_path")
            params["source_path"] = command.source_path
        if command.start_time:
            conditions.append("(collected_end IS NULL OR collected_end >= :start_time)")
            params["start_time"] = command.start_time
        if command.end_time:
            conditions.append("(collected_start IS NULL OR collected_start <= :end_time)")
            params["end_time"] = command.end_time
        result = await self._session.execute(
            text(
                f"""
                SELECT evidence_id, collector_id, source_path, source_object, evidence_status,
                       media_type, collected_start, collected_end, structured_data, quality,
                       failure_reason, sha256
                FROM evidence_item
                WHERE {" AND ".join(conditions)}
                ORDER BY created_at DESC, evidence_id
                LIMIT :limit
                """
            ),
            params,
        )
        rows = [dict(row) for row in result.mappings().all()]
        evidence: list[dict[str, Any]] = []
        statuses: set[str] = set()
        for row in rows:
            statuses.add(row["evidence_status"])
            if command.query_type == "evidence_status":
                evidence.append(self._evidence_result(row, row["failure_reason"]))
                continue
            if row["evidence_status"] != "available":
                continue
            value = self._query_value(row["structured_data"], command)
            if value is not None:
                evidence.append(self._evidence_result(row, value))
        status = "available" if evidence else self._missing_status(statuses)
        return {
            "query_id": uuid.uuid4(),
            "status": status,
            "evidence": evidence,
            "trace_id": get_current_trace_id() or secrets.token_hex(16),
        }

    @staticmethod
    def _query_value(data: Any, command: EvidenceQueryRequest) -> Any | None:
        if command.query_type in {"log", "command_output"}:
            text_value = _flatten_text(data)
            if command.keyword and command.keyword.casefold() not in text_value.casefold():
                return None
            return text_value[:4000]
        if command.query_type == "json":
            return _resolve_json_path(data, command.json_path or command.field or "")
        if command.query_type == "metric":
            value = _resolve_json_path(data, command.field or command.json_path or "")
            numbers = _numbers(value)
            if not numbers:
                return None
            return {"min": min(numbers), "max": max(numbers), "count": len(numbers)}
        return data

    @staticmethod
    def _evidence_result(row: dict[str, Any], value: Any) -> dict[str, Any]:
        return {
            "evidence_ref": str(row["evidence_id"]),
            "source": row["source_path"],
            "object": row["source_object"],
            "time": row["collected_end"] or row["collected_start"],
            "field": None,
            "value": value,
            "quality": row["quality"],
            "collector_id": row["collector_id"],
            "sha256": row["sha256"],
        }

    @staticmethod
    def _missing_status(statuses: set[str]) -> str:
        precedence = (
            "collection_failed",
            "out_of_time_range",
            "not_applicable",
            "unreadable",
            "skipped_by_user",
        )
        return next((item for item in precedence if item in statuses), "missing")


class OfflineAnalysisService:
    """生成不可变评估、Diagnosis Run（诊断运行）、报告与一次补采计划。"""

    def __init__(self, session: AsyncSession):
        self._session = session
        self._cdd_decision: Any = None
        self._cdd_assessments: dict[str, Any] = {}

    async def assess_and_diagnose(self, *, tenant_id: str, session_id: str, trace_id: str) -> dict[str, Any]:
        """按当前 ready Bundle 集合幂等创建评估和诊断运行。"""

        session_row = await self._lock_session(tenant_id, session_id)
        bundles = await self._ready_bundles(tenant_id, session_id)
        if not bundles:
            raise DiagnosisError(code="NO_READY_EVIDENCE", message="当前会话没有可诊断证据包", http_status=409)
        plan = await self._load_current_plan(session_id)
        plan_items = await self._load_plan_items(plan["plan_id"])
        evidence = await self._load_evidence(tenant_id, session_id)
        profile_snapshot = {
            "profile_name": plan["profile_name"],
            "profile_revision": plan["profile_revision"],
            "profile_version": plan["profile_version"],
            "profile_checksum": plan["profile_checksum"],
            "plan_id": str(plan["plan_id"]),
        }
        bundle_ids = [str(row["bundle_id"]) for row in bundles]
        assessment_values = self._calculate_assessment(plan_items, evidence, bundle_ids, profile_snapshot)
        assessment_id = await self._ensure_assessment(
            tenant_id=tenant_id,
            session_id=session_id,
            trace_id=trace_id,
            **assessment_values,
        )
        run_manifest = await self._run_manifest(
            session_row=session_row,
            plan=plan,
            bundles=bundles,
            assessment_id=assessment_id,
        )
        manifest_hash = _canonical_hash(run_manifest)
        existing_run = await self._session.execute(
            text(
                """
                SELECT run_id FROM diagnosis_run
                WHERE session_id = :session_id AND run_manifest_sha256 = :manifest_hash
                """
            ),
            {"session_id": session_id, "manifest_hash": manifest_hash},
        )
        existing_run_id = existing_run.scalar_one_or_none()
        if existing_run_id:
            return await self._get_run(existing_run_id, tenant_id)

        sequence_result = await self._session.execute(
            text("SELECT COALESCE(MAX(run_sequence), 0) + 1 FROM diagnosis_run WHERE session_id = :session_id"),
            {"session_id": session_id},
        )
        run_sequence = sequence_result.scalar_one()
        if run_sequence > 2:
            raise DiagnosisError(
                code="DIAGNOSIS_RUN_LIMIT_REACHED", message="P0 最多允许初始和补采两次分析", http_status=409
            )
        run_id = uuid.uuid4()
        await self._session.execute(
            text(
                """
                INSERT INTO diagnosis_run (
                    run_id, tenant_id, session_id, assessment_id, run_sequence, status,
                    selected_category, run_manifest, run_manifest_sha256,
                    conclusion_policy_version, matcher_version, agent_version, model_version, trace_id
                ) VALUES (
                    :run_id, :tenant_id, :session_id, :assessment_id, :run_sequence, 'running',
                    :selected_category, CAST(:run_manifest AS jsonb), :manifest_hash,
                    :policy_version, :matcher_version, :agent_version, NULL, :trace_id
                )
                """
            ),
            {
                "run_id": run_id,
                "tenant_id": tenant_id,
                "session_id": session_id,
                "assessment_id": assessment_id,
                "run_sequence": run_sequence,
                "selected_category": session_row["selected_category"],
                "run_manifest": json.dumps(run_manifest, ensure_ascii=False, sort_keys=True),
                "manifest_hash": manifest_hash,
                "policy_version": CONCLUSION_POLICY_VERSION,
                "matcher_version": MATCHER_VERSION,
                "agent_version": AGENT_VERSION,
                "trace_id": trace_id,
            },
        )
        candidates, evaluations = await self._evaluate_kbds(session_row, plan, evidence)
        for evaluation in evaluations:
            await self._insert_evaluation(run_id, trace_id, evaluation)
        for candidate in candidates:
            await self._insert_candidate(run_id, trace_id, candidate)
        assessment = {**assessment_values, "assessment_id": assessment_id}
        conclusion = _conclusion(assessment, candidates, cdd_decision=getattr(self, "_cdd_decision", None))
        supplement_id = None
        if (
            settings.DIAGNOSIS_ENABLE_AUTOMATIC_SUPPLEMENT
            and conclusion["level"] in {"Probable", "Suspected", "Insufficient"}
            and session_row["supplement_count"] < 1
        ):
            supplement_id = await self._create_supplement(
                tenant_id=tenant_id,
                session_row=session_row,
                run_id=run_id,
                plan=plan,
                plan_items=plan_items,
                evidence=evidence,
                assessment=assessment,
                evaluations=evaluations,
                trace_id=trace_id,
            )
        report_id = await self._create_report(
            tenant_id=tenant_id,
            session_row=session_row,
            run_id=run_id,
            run_sequence=run_sequence,
            candidates=candidates,
            evaluations=evaluations,
            assessment=assessment,
            conclusion=conclusion,
            supplement_id=supplement_id,
            trace_id=trace_id,
        )
        await self._session.execute(
            text(
                """
                UPDATE diagnosis_run
                SET status = 'completed', resolved_category = :resolved_category,
                    completed_at = CURRENT_TIMESTAMP
                WHERE run_id = :run_id
                """
            ),
            {
                "run_id": run_id,
                "resolved_category": candidates[0]["category_id"] if candidates else session_row["selected_category"],
            },
        )
        target_status = "supplement_required" if supplement_id else "review_pending"
        supplement_increment = 1 if supplement_id else 0
        await self._session.execute(
            text(
                """
                UPDATE diagnosis_session
                SET status = :target_status,
                    supplement_count = supplement_count + :supplement_increment,
                    resolved_category = :resolved_category,
                    version = version + 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE session_id = :session_id
                """
            ),
            {
                "session_id": session_id,
                "target_status": target_status,
                "supplement_increment": supplement_increment,
                "resolved_category": candidates[0]["category_id"] if candidates else session_row["selected_category"],
            },
        )
        if run_sequence > 1:
            await self._session.execute(
                text(
                    """
                    UPDATE diagnosis_report
                    SET publish_status = 'superseded', version = version + 1, updated_at = CURRENT_TIMESTAMP
                    WHERE session_id = :session_id AND report_id <> :report_id
                      AND publish_status IN ('draft', 'review_pending', 'engineer_confirmed', 'customer_published')
                    """
                ),
                {"session_id": session_id, "report_id": report_id},
            )
            await self._session.execute(
                text(
                    """
                    UPDATE supplement_plan
                    SET status = 'completed', updated_at = CURRENT_TIMESTAMP
                    WHERE session_id = :session_id
                    """
                ),
                {"session_id": session_id},
            )
        return await self._get_run(run_id, tenant_id)

    async def get_assessment(self, *, actor: ActorContext, session_id: str) -> dict[str, Any]:
        """读取最新证据评估，并用其不可变 Bundle（证据包）补全缺失原因。"""

        self._require_read_role(actor)
        result = await self._session.execute(
            text(
                """
                SELECT * FROM evidence_assessment
                WHERE tenant_id = :tenant_id AND session_id = :session_id
                ORDER BY created_at DESC LIMIT 1
                """
            ),
            {"tenant_id": actor.tenant_id, "session_id": session_id},
        )
        row = result.mappings().one_or_none()
        if row is None:
            raise DiagnosisError(code="ASSESSMENT_NOT_FOUND", message="证据评估尚未生成", http_status=404)
        assessment = dict(row)
        evidence_result = await self._session.execute(
            text(
                """
                SELECT e.collector_id, e.evidence_status, e.failure_reason, e.source_path,
                       e.structured_data, i.display_name
                FROM evidence_item e
                LEFT JOIN collection_plan_item i ON i.item_id = e.collection_plan_item_id
                WHERE e.tenant_id = :tenant_id AND e.session_id = :session_id
                  AND e.bundle_id IN (
                      SELECT value::uuid
                      FROM jsonb_array_elements_text(CAST(:bundle_ids AS jsonb)) AS bundle_id_text(value)
                  )
                ORDER BY e.created_at, e.evidence_id
                """
            ),
            {
                "tenant_id": actor.tenant_id,
                "session_id": session_id,
                "bundle_ids": json.dumps(assessment["bundle_ids"]),
            },
        )
        evidence_by_collector: dict[str, list[dict[str, Any]]] = defaultdict(list)
        display_name_by_collector: dict[str, str] = {}
        for evidence_row in evidence_result.mappings().all():
            evidence_by_collector[evidence_row["collector_id"]].append(dict(evidence_row))
            if evidence_row["display_name"]:
                display_name_by_collector[evidence_row["collector_id"]] = evidence_row["display_name"]
        assessment["missing_evidence"] = [
            {
                **item,
                "display_name": item.get("display_name") or display_name_by_collector.get(item["collector_id"]),
                **_missing_evidence_metadata(evidence_by_collector.get(item["collector_id"], [])),
            }
            for item in assessment["missing_evidence"]
        ]
        return assessment

    async def list_runs(self, *, actor: ActorContext, session_id: str) -> list[dict[str, Any]]:
        """列出会话全部不可变诊断运行。"""

        self._require_read_role(actor)
        publication_clause = ""
        if actor.roles.isdisjoint(ANALYSIS_ROLES):
            publication_clause = """
                AND EXISTS (
                    SELECT 1 FROM diagnosis_report p
                    WHERE p.run_id = diagnosis_run.run_id
                      AND p.tenant_id = diagnosis_run.tenant_id
                      AND p.publish_status = 'customer_published'
                )
            """
        result = await self._session.execute(
            text(
                f"""
                SELECT * FROM diagnosis_run
                WHERE tenant_id = :tenant_id AND session_id = :session_id
                  {publication_clause}
                ORDER BY run_sequence
                """
            ),
            {"tenant_id": actor.tenant_id, "session_id": session_id},
        )
        return [dict(row) for row in result.mappings().all()]

    async def list_signal_evaluations(
        self,
        *,
        actor: ActorContext,
        session_id: str,
        run_id: str,
    ) -> list[dict[str, Any]]:
        """读取指定运行的三态 Signal（信号）评估明细。"""

        self._require_read_role(actor)
        await self._assert_run_visible(actor=actor, session_id=session_id, run_id=run_id)
        result = await self._session.execute(
            text(
                """
                SELECT e.*
                FROM signal_evaluation e
                JOIN diagnosis_run r ON r.run_id = e.run_id
                WHERE r.tenant_id = :tenant_id
                  AND r.session_id = :session_id
                  AND r.run_id = :run_id
                ORDER BY e.required_for_conclusion DESC, e.signal_id
                """
            ),
            {"tenant_id": actor.tenant_id, "session_id": session_id, "run_id": run_id},
        )
        rows = [dict(row) for row in result.mappings().all()]
        if actor.roles.isdisjoint(ANALYSIS_ROLES):
            for row in rows:
                row["matcher_snapshot"] = None
        return rows

    async def list_diagnosis_candidates(
        self,
        *,
        actor: ActorContext,
        session_id: str,
        run_id: str,
    ) -> list[dict[str, Any]]:
        """读取指定运行的 KBD（知识库文档）诊断候选快照。"""

        self._require_read_role(actor)
        await self._assert_run_visible(actor=actor, session_id=session_id, run_id=run_id)
        result = await self._session.execute(
            text(
                """
                SELECT c.*
                FROM diagnosis_candidate c
                JOIN diagnosis_run r ON r.run_id = c.run_id
                WHERE r.tenant_id = :tenant_id
                  AND r.session_id = :session_id
                  AND r.run_id = :run_id
                ORDER BY c.score DESC, c.candidate_id
                """
            ),
            {"tenant_id": actor.tenant_id, "session_id": session_id, "run_id": run_id},
        )
        rows = [dict(row) for row in result.mappings().all()]
        if actor.roles.isdisjoint(ANALYSIS_ROLES):
            for row in rows:
                row["kbd_snapshot"] = {
                    "support_id": row["support_id"],
                    "title": row["title"],
                    "category_id": row["category_id"],
                }
        return rows

    async def list_reports(self, *, actor: ActorContext, session_id: str) -> list[dict[str, Any]]:
        """客户只能看到已发布报告，其余可信角色可看完整版本。"""

        self._require_read_role(actor)
        status_clause = ""
        if actor.roles.isdisjoint(ANALYSIS_ROLES):
            status_clause = """
                AND (
                    publish_status = 'customer_published'
                    OR (
                        publish_status = 'superseded'
                        AND EXISTS (
                            SELECT 1 FROM diagnosis_report_revision revision
                            WHERE revision.report_id = diagnosis_report.report_id
                              AND revision.action = 'publish'
                        )
                    )
                )
            """
        result = await self._session.execute(
            text(
                f"""
                SELECT * FROM diagnosis_report
                WHERE tenant_id = :tenant_id AND session_id = :session_id {status_clause}
                ORDER BY report_sequence DESC
                """
            ),
            {"tenant_id": actor.tenant_id, "session_id": session_id},
        )
        return [dict(row) for row in result.mappings().all()]

    async def review_report(
        self,
        *,
        actor: ActorContext,
        session_id: str,
        report_id: str,
        expected_version: int,
        command: ReportReviewRequest,
    ) -> dict[str, Any]:
        """按角色、状态机和乐观锁审核报告。"""

        if actor.roles.isdisjoint(ANALYSIS_ROLES):
            raise DiagnosisError(code="FORBIDDEN", message="当前角色无权审核诊断报告", http_status=403)
        result = await self._session.execute(
            text(
                """
                SELECT * FROM diagnosis_report
                WHERE tenant_id = :tenant_id AND session_id = :session_id AND report_id = :report_id
                FOR UPDATE
                """
            ),
            {"tenant_id": actor.tenant_id, "session_id": session_id, "report_id": report_id},
        )
        report = result.mappings().one_or_none()
        if report is None:
            raise DiagnosisError(code="DIAGNOSIS_REPORT_NOT_FOUND", message="诊断报告不存在", http_status=404)
        report = dict(report)
        if report["version"] != expected_version:
            raise DiagnosisError(
                code="REPORT_VERSION_CONFLICT",
                message="诊断报告已被其他审核操作更新",
                http_status=409,
                details={"current_version": report["version"]},
            )
        transition = {
            ("draft", "submit_review"): "review_pending",
            ("review_pending", "confirm"): "engineer_confirmed",
            ("engineer_confirmed", "publish"): "customer_published",
            ("review_pending", "reject"): "rejected",
            ("review_pending", "return_to_draft"): "draft",
        }.get((report["publish_status"], command.action))
        if transition is None:
            raise DiagnosisError(
                code="INVALID_REPORT_TRANSITION",
                message="诊断报告状态转换不合法",
                http_status=409,
                details={"status": report["publish_status"], "action": command.action},
            )
        if (
            command.action == "confirm"
            and report["diagnosis_level"] in {"Insufficient", "Conflicted"}
            and not actor.has_any_role("domain_expert", "platform_admin")
        ):
            raise DiagnosisError(code="DOMAIN_EXPERT_REQUIRED", message="该报告必须由领域专家审核", http_status=403)
        before = _report_snapshot(report)
        summary = command.summary if command.summary is not None else report["summary"]
        recovery = (
            command.recommended_recovery if command.recommended_recovery is not None else report["recommended_recovery"]
        )
        trace_id = get_current_trace_id() or secrets.token_hex(16)
        await self._session.execute(
            text(
                """
                UPDATE diagnosis_report
                SET publish_status = :status, summary = :summary,
                    recommended_recovery = CAST(:recovery AS jsonb),
                    version = version + 1, trace_id = :trace_id, updated_at = CURRENT_TIMESTAMP
                WHERE report_id = :report_id
                """
            ),
            {
                "report_id": report_id,
                "status": transition,
                "summary": summary,
                "recovery": json.dumps(recovery, ensure_ascii=False),
                "trace_id": trace_id,
            },
        )
        updated_result = await self._session.execute(
            text("SELECT * FROM diagnosis_report WHERE report_id = :report_id"),
            {"report_id": report_id},
        )
        updated = dict(updated_result.mappings().one())
        await self._session.execute(
            text(
                """
                INSERT INTO diagnosis_report_revision (
                    report_id, action, actor_id, actor_roles, reason,
                    before_snapshot, after_snapshot, trace_id
                ) VALUES (
                    :report_id, :action, :actor_id, CAST(:actor_roles AS jsonb), :reason,
                    CAST(:before_snapshot AS jsonb), CAST(:after_snapshot AS jsonb), :trace_id
                )
                """
            ),
            {
                "report_id": report_id,
                "action": command.action,
                "actor_id": actor.user_id,
                "actor_roles": json.dumps(sorted(actor.roles)),
                "reason": command.reason,
                "before_snapshot": json.dumps(before, ensure_ascii=False, default=str),
                "after_snapshot": json.dumps(_report_snapshot(updated), ensure_ascii=False, default=str),
                "trace_id": trace_id,
            },
        )
        if transition == "customer_published":
            await self._session.execute(
                text(
                    """
                    UPDATE diagnosis_session
                    SET status = 'published', version = version + 1, updated_at = CURRENT_TIMESTAMP
                    WHERE session_id = :session_id AND status = 'review_pending'
                    """
                ),
                {"session_id": session_id},
            )
        return updated

    async def _assert_run_visible(self, *, actor: ActorContext, session_id: str, run_id: str) -> None:
        """校验运行归属；客户角色只能读取已发布报告对应的运行。"""

        publication_clause = ""
        if actor.roles.isdisjoint(ANALYSIS_ROLES):
            publication_clause = """
                AND EXISTS (
                    SELECT 1
                    FROM diagnosis_report p
                    WHERE p.run_id = r.run_id
                      AND p.tenant_id = r.tenant_id
                      AND p.publish_status = 'customer_published'
                )
            """
        result = await self._session.execute(
            text(
                f"""
                SELECT 1
                FROM diagnosis_run r
                WHERE r.tenant_id = :tenant_id
                  AND r.session_id = :session_id
                  AND r.run_id = :run_id
                  {publication_clause}
                """
            ),
            {"tenant_id": actor.tenant_id, "session_id": session_id, "run_id": run_id},
        )
        if result.scalar_one_or_none() is None:
            raise DiagnosisError(
                code="DIAGNOSIS_RUN_NOT_FOUND",
                message="诊断运行不存在或当前身份不可见",
                http_status=404,
            )

    async def _evaluate_kbds(
        self, session_row: dict[str, Any], collection_plan: dict[str, Any], evidence: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """使用共享 CDD 内核评估 KBD 候选。

        流程：
        1. 加载分类内已发布 KBD → 构建 KBD 对象
        2. compile_signal_plan(candidates) → SignalPlan
        3. 批量求值所有信号（通过 _evaluate_signal）
        4. 将离线结果映射为 CDD SignalOutcome
        5. reduce_candidates(plan, assessments)
        6. decide_conclusion(assessments) → CDD 四级结论
        7. CDD 四级 → 离线五级映射
        """
        from shared.cdd import (
            AcquisitionRunResult,
            CandidateState,
            SignalOutcome,
            compile_signal_plan,
            decide_conclusion,
            execute_acquisition_plan,
        )
        from shared.cdd.candidate_reducer import initial_assessments, reduce_candidates
        from shared.cdd.kbd_model import kbd_from_dict

        ruleset = list(collection_plan.get("kbd_ruleset_snapshot") or [])
        if not ruleset:
            raise DiagnosisError(
                code="KBD_RULESET_SNAPSHOT_MISSING",
                message="采集计划缺少不可变 KBD 规则快照，请重新生成采集计划和制品",
                http_status=409,
            )

        # 1. 只从采集计划的不可变快照构建 KBD 对象，禁止运行时回读当前 KBD/映射。
        kbd_rows: list[dict[str, Any]] = []
        signal_mappings: list[dict[str, Any]] = []
        for item in ruleset:
            row = {
                "id": item.get("kbd_id"),
                "support_id": item.get("support_id"),
                "title": item.get("title"),
                "root_cause": item.get("root_cause"),
                "solution": item.get("solution"),
                "operational_impact": item.get("operational_impact"),
                "recommendations": item.get("recommendations"),
                "category_id": item.get("category_id"),
                "metadata": item.get("metadata") or {},
                "signals_json": {
                    "schema_version": 2,
                    "signals": item.get("signals") or [],
                    "verification_contract": item.get("verification_contract") or {},
                    "generation_metadata": item.get("generation_metadata") or {},
                    "publish_validation": item.get("publish_validation") or {},
                },
                "updated_at": item.get("updated_at"),
                "resource_revision": item.get("revision"),
                "resource_checksum": item.get("resource_checksum") or item.get("checksum"),
            }
            signals = item.get("signals")
            if not isinstance(signals, list) or not signals:
                continue
            frozen_mappings = item.get("offline_signal_mappings")
            if not isinstance(frozen_mappings, list):
                raise DiagnosisError(
                    code="OFFLINE_MAPPING_SNAPSHOT_MISSING",
                    message="采集计划使用旧版映射快照，请重新生成采集计划和制品",
                    http_status=409,
                    details={"kbd_id": item.get("kbd_id")},
                )
            signal_mappings.extend(mapping for mapping in frozen_mappings if isinstance(mapping, dict))
            kbd_rows.append(row)

        if not kbd_rows:
            return [], []

        kbd_objects = [kbd_from_dict(row_to_kbd_dict(row)) for row in kbd_rows]

        # 2. 编译信号计划
        plan = compile_signal_plan(kbd_objects, snapshot_id=str(session_row.get("session_id", "offline")))

        # 3. 初始化评估
        assessments = initial_assessments(plan)

        # 4. 通过共享 AcquisitionProvider 运行器执行离线 Evidence 查询。
        rows_by_id = {str(row["id"]): row for row in kbd_rows}
        service = self

        class FrozenEvidenceAcquisitionProvider:
            """把已冻结证据映射适配为共享采集提供器。"""

            async def acquire(self, acquisition) -> AcquisitionRunResult:
                outcomes: dict[str, SignalOutcome] = {}
                evaluations: list[dict[str, Any]] = []
                produced: set[str] = set()
                for ref in acquisition.signal_refs:
                    row = rows_by_id[ref.kbd_id]
                    evaluation = service._evaluate_signal(
                        int(row["id"]),
                        int(row["resource_revision"]),
                        row["support_id"],
                        row["category_id"],
                        ref.signal,
                        evidence,
                        signal_mappings,
                    )
                    evaluations.append(evaluation)
                    if evaluation["state"] == "MATCHED":
                        outcomes[ref.ref_id] = SignalOutcome.SATISFIED
                    elif evaluation["state"] == "NOT_MATCHED":
                        outcomes[ref.ref_id] = SignalOutcome.CONTRADICTED
                    else:
                        outcomes[ref.ref_id] = SignalOutcome.UNKNOWN
                    if evaluation["evidence_status"] == "available":
                        produced.update(ref.produces)
                return AcquisitionRunResult(
                    outcomes=outcomes,
                    produced_variables=frozenset(produced),
                    evaluations=tuple(evaluations),
                )

        context_snapshot = collection_plan.get("context_snapshot") or {}
        initial_variables = set(context_snapshot) if isinstance(context_snapshot, dict) else set()
        _variables, all_evaluations = await execute_acquisition_plan(
            plan,
            assessments,
            FrozenEvidenceAcquisitionProvider(),
            available_variables=initial_variables,
        )

        # 5. 执行 CDD 候选归约
        reduce_candidates(plan, assessments, finalize=True)

        # 6. 执行 CDD 结论门禁
        decision = decide_conclusion(assessments)

        # 7. CDD 四级 → 离线五级结论映射
        cdd_level = decision.level
        # 构建离线 candidates 列表
        offline_candidates: list[dict[str, Any]] = []
        for row in kbd_rows:
            kbd_id = str(row["id"])
            assessment = assessments.get(kbd_id)
            if assessment is None:
                continue

            # 统计每个 KBD 的信号评估结果
            kbd_evals = [e for e in all_evaluations if e["support_id"] == row["support_id"]]
            matched = sum(1 for e in kbd_evals if e["state"] == "MATCHED")
            not_matched = sum(1 for e in kbd_evals if e["state"] == "NOT_MATCHED")
            unknown = sum(1 for e in kbd_evals if e["state"] == "UNKNOWN")
            total = len(kbd_evals)
            coverage = (matched + not_matched) / total if total > 0 else 0.0

            # 根据 CDD 候选状态计算分数
            cdd_state = assessment.state
            if cdd_state == CandidateState.SUPPORTED:
                score = 0.95
            elif cdd_state == CandidateState.REJECTED:
                score = max(0.0, 0.3 - not_matched * 0.1)
            elif cdd_state == CandidateState.INCONCLUSIVE:
                score = max(0.0, min(0.7, (matched / total) * 0.6 if total > 0 else 0.3))
            elif cdd_state == CandidateState.NOT_EXECUTABLE:
                score = 0.1
            else:
                score = max(0.0, min(1.0, (matched / total) * 0.75 + coverage * 0.25 - not_matched * 0.15))

            offline_candidates.append(
                {
                    "kbd_id": row["id"],
                    "support_id": row["support_id"],
                    "title": row["title"],
                    "category_id": row["category_id"],
                    "score": round(score, 6),
                    "matched_count": matched,
                    "not_matched_count": not_matched,
                    "unknown_count": unknown,
                    "signal_coverage": round(coverage, 6),
                    "root_cause": row["root_cause"],
                    "solution": row["solution"],
                    "operational_impact": row["operational_impact"],
                    "recommendations": row["recommendations"],
                    "cdd_state": str(cdd_state.value),
                    "cdd_level": str(cdd_level.value),
                    "snapshot": {
                        "id": row["id"],
                        "support_id": row["support_id"],
                        "title": row["title"],
                        "category_id": row["category_id"],
                        "metadata": row["metadata"],
                        "signals_json": row["signals_json"],
                        "resource_revision": row["resource_revision"],
                        "resource_checksum": row["resource_checksum"],
                        "updated_at": str(row["updated_at"]),
                    },
                }
            )

        # 按 CDD 状态 + 分数排序
        state_rank = {
            CandidateState.SUPPORTED.value: 0,
            CandidateState.INCONCLUSIVE.value: 1,
            CandidateState.CANDIDATE.value: 2,
            CandidateState.REJECTED.value: 3,
            CandidateState.NOT_EXECUTABLE.value: 4,
        }
        offline_candidates.sort(
            key=lambda item: (
                state_rank.get(item["cdd_state"], 9),
                -item["score"],
                item["unknown_count"],
                str(item["support_id"]),
            )
        )
        # 存储 CDD 结论信息供 _conclusion 使用
        self._cdd_decision = decision
        self._cdd_assessments = assessments

        return offline_candidates, all_evaluations

    def _evaluate_signal(
        self,
        kbd_id: int,
        kbd_revision: int,
        support_id: str,
        category_id: str | None,
        signal: dict[str, Any],
        evidence: list[dict[str, Any]],
        signal_mappings: list[dict[str, Any]],
    ) -> dict[str, Any]:
        acquire = signal.get("acquire") or {}
        explicit_collector = acquire.get("collector_id") or (acquire.get("offline") or {}).get("collector_id")
        acquire_tool = str(acquire.get("tool") or "")
        acquire_command = str((acquire.get("args") or {}).get("command") or "*")
        signal_id = str(signal.get("id") or _canonical_hash(signal)[:16])
        mapped = [
            item
            for item in signal_mappings
            if int(item["source_kbd_id"]) == kbd_id
            and int(item["source_kbd_revision"]) == kbd_revision
            and item["source_signal_id"] == signal_id
            and item["acquire_tool"] == acquire_tool
        ]
        mapped.sort(
            key=lambda item: (
                item["category_scope"] != category_id,
                item["command_scope"] != acquire_command,
                item["priority"],
                item["collector_id"],
            )
        )
        mapped_collector_ids = [item["collector_id"] for item in mapped]
        # KBD 中的历史 collector_id 只能收窄已审批的精确映射，不能绕过同步编译与治理链路。
        collector_ids = mapped_collector_ids
        if explicit_collector:
            collector_ids = [explicit_collector] if explicit_collector in mapped_collector_ids else []
        collector_ids = list(dict.fromkeys(item for item in collector_ids if item))
        eligible = [item for item in evidence if item["collector_id"] in collector_ids] if collector_ids else []
        available = [item for item in eligible if item["evidence_status"] == "available"]
        full_signal_id = f"kbd:{support_id}:{signal_id}"
        matcher = signal.get("match")
        matcher_snapshot = (
            {
                **matcher,
                "_offline_mapping": {
                    "acquire_tool": acquire_tool,
                    "acquire_command": acquire_command,
                    "collector_ids": collector_ids,
                },
            }
            if isinstance(matcher, dict)
            else {
                "_offline_mapping": {
                    "acquire_tool": acquire_tool,
                    "acquire_command": acquire_command,
                    "collector_ids": collector_ids,
                }
            }
        )
        required = bool((signal.get("review") or {}).get("require_human_confirm"))
        if not available:
            status = OfflineEvidenceProvider._missing_status({item["evidence_status"] for item in eligible})
            reason = (
                "KBD 在线采集工具尚未配置 Offline Signal Mapping（离线信号映射）"
                if acquire_tool and not collector_ids
                else "所需离线证据缺失、失败或不可读"
            )
            return {
                "support_id": support_id,
                "signal_id": full_signal_id,
                "state": "UNKNOWN",
                "reason": reason,
                "required_for_conclusion": required,
                "evidence_status": status,
                "evidence_refs": [],
                "matcher_snapshot": matcher_snapshot,
            }
        refs = [str(item["evidence_id"]) for item in available]
        if matcher is None:
            return {
                "support_id": support_id,
                "signal_id": full_signal_id,
                "state": "MATCHED",
                "reason": "信号所需证据存在，产出变量模式由结构化证据支持",
                "required_for_conclusion": required,
                "evidence_status": "available",
                "evidence_refs": refs,
                "matcher_snapshot": matcher_snapshot,
            }
        outcomes = [_evaluate_matcher(matcher, item["structured_data"]) for item in available]
        determinate = [item for item in outcomes if item is not None]
        if not determinate:
            state, reason = "UNKNOWN", "证据存在，但 matcher 无法解析证据内容"
        elif any(determinate) and not all(determinate):
            state, reason = "UNKNOWN", "不同证据项的确定性 matcher 结果冲突"
        elif all(determinate):
            state, reason = "MATCHED", "确定性 matcher 命中"
        else:
            state, reason = "NOT_MATCHED", "证据存在且确定性 matcher 明确不命中"
        return {
            "support_id": support_id,
            "signal_id": full_signal_id,
            "state": state,
            "reason": reason,
            "required_for_conclusion": required,
            "evidence_status": "available",
            "evidence_refs": refs,
            "matcher_snapshot": matcher_snapshot,
        }

    async def _load_signal_mappings(self, categories: list[str]) -> list[dict[str, Any]]:
        """加载本次规则快照可用的在线工具到离线 Collector 映射。"""

        result = await self._session.execute(
            text(
                """
                SELECT mapping_id, source_kbd_id, source_kbd_revision, source_signal_id,
                       execution_contract_checksum, acquire_tool, category_scope, command_scope,
                       collector_id, query_type, field_mapping, priority
                FROM offline_signal_collector_mapping
                WHERE is_enabled = true
                  AND source_kbd_id IS NOT NULL
                  AND (category_scope = '*' OR category_scope = ANY(CAST(:categories AS varchar[])))
                ORDER BY source_kbd_id, source_kbd_revision, source_signal_id, priority, collector_id
                """
            ),
            {"categories": categories or [""]},
        )
        return [dict(row) for row in result.mappings().all()]

    async def _candidate_categories(self, selected: str | None) -> list[str]:
        if not selected:
            return []
        result = await self._session.execute(
            text(
                """
                WITH selected AS (
                    SELECT id, parent_id FROM kb_category WHERE code = :selected
                )
                SELECT code FROM kb_category
                WHERE code = :selected
                   OR parent_id = (SELECT parent_id FROM selected)
                   OR id = (SELECT parent_id FROM selected)
                ORDER BY CASE WHEN code = :selected THEN 0 ELSE 1 END, code
                LIMIT 32
                """
            ),
            {"selected": selected},
        )
        return [row[0] for row in result.all()]

    @staticmethod
    def _calculate_assessment(
        plan_items: list[dict[str, Any]],
        evidence: list[dict[str, Any]],
        bundle_ids: list[str],
        profile_snapshot: dict[str, Any],
    ) -> dict[str, Any]:
        active = [item for item in plan_items if item["activation_state"] == "active"]
        mandatory = [item for item in active if item["required_level"] == "mandatory"]
        details: list[dict[str, Any]] = []
        missing: list[dict[str, Any]] = []
        for item in active:
            target = item["target"] or {}
            matches = [
                evidence_item
                for evidence_item in evidence
                if evidence_item["collector_id"] == item["collector_id"]
                and (
                    str(evidence_item.get("collection_plan_item_id") or "") == str(item["item_id"])
                    or _source_matches(target, evidence_item["source_object"] or {})
                )
            ]
            available = any(match["evidence_status"] == "available" for match in matches)
            entry = {
                "plan_item_id": str(item["item_id"]),
                "collector_id": item["collector_id"],
                "required_level": item["required_level"],
                "target": target,
                "available": available,
                "evidence_refs": [str(match["evidence_id"]) for match in matches],
            }
            details.append(entry)
            if not available and item["required_level"] == "mandatory":
                status = OfflineEvidenceProvider._missing_status({match["evidence_status"] for match in matches})
                missing.append(
                    {
                        "plan_item_id": str(item["item_id"]),
                        "collector_id": item["collector_id"],
                        "display_name": item["display_name"],
                        "target": target,
                        "status": status,
                        "reason": "mandatory_collection_item_unavailable",
                        "impact": "缺少该必需采集项，无法确认对应 KBD 的根因结论。",
                        **_missing_evidence_metadata(matches),
                    }
                )
        available_count = sum(item["available"] for item in details)
        mandatory_available = sum(item["available"] for item in details if item["required_level"] == "mandatory")
        score = round(100 * available_count / max(1, len(active)))
        ready = len(mandatory) > 0 and mandatory_available == len(mandatory)
        input_payload = {
            "bundle_ids": bundle_ids,
            "profile_snapshot": profile_snapshot,
            "details": details,
            "algorithm_version": ASSESSMENT_ALGORITHM_VERSION,
        }
        return {
            "bundle_ids": bundle_ids,
            "profile_snapshot": profile_snapshot,
            "input_hash": _canonical_hash(input_payload),
            "algorithm_version": ASSESSMENT_ALGORITHM_VERSION,
            "completeness_score": score,
            "mandatory_total": len(mandatory),
            "mandatory_available": mandatory_available,
            "missing_evidence": missing,
            "diagnosable_scope": ["failure_domain", "candidate_root_cause"] if ready else [],
            "non_diagnosable_scope": [] if not missing else ["confirmed_root_cause"],
            "ready_for_diagnosis": ready,
            "calculation_details": {
                "items": details,
                "active_total": len(active),
                "active_available": available_count,
                "formula": "round(100 * active_available / active_total); ready iff every mandatory item is available",
            },
        }

    async def _ensure_assessment(self, *, tenant_id: str, session_id: str, trace_id: str, **values) -> uuid.UUID:
        assessment_id = uuid.uuid4()
        result = await self._session.execute(
            text(
                """
                INSERT INTO evidence_assessment (
                    assessment_id, tenant_id, session_id, bundle_ids, profile_snapshot, input_hash,
                    algorithm_version, completeness_score, mandatory_total, mandatory_available,
                    missing_evidence, diagnosable_scope, non_diagnosable_scope,
                    ready_for_diagnosis, calculation_details, trace_id
                ) VALUES (
                    :assessment_id, :tenant_id, :session_id, CAST(:bundle_ids AS jsonb),
                    CAST(:profile_snapshot AS jsonb), :input_hash, :algorithm_version,
                    :completeness_score, :mandatory_total, :mandatory_available,
                    CAST(:missing_evidence AS jsonb), CAST(:diagnosable_scope AS jsonb),
                    CAST(:non_diagnosable_scope AS jsonb), :ready_for_diagnosis,
                    CAST(:calculation_details AS jsonb), :trace_id
                )
                ON CONFLICT (session_id, input_hash) DO NOTHING
                RETURNING assessment_id
                """
            ),
            {
                "assessment_id": assessment_id,
                "tenant_id": tenant_id,
                "session_id": session_id,
                "trace_id": trace_id,
                **{
                    key: json.dumps(value, ensure_ascii=False)
                    if key
                    in {
                        "bundle_ids",
                        "profile_snapshot",
                        "missing_evidence",
                        "diagnosable_scope",
                        "non_diagnosable_scope",
                        "calculation_details",
                    }
                    else value
                    for key, value in values.items()
                },
            },
        )
        created = result.scalar_one_or_none()
        if created:
            return created
        existing = await self._session.execute(
            text(
                "SELECT assessment_id FROM evidence_assessment WHERE session_id = :session_id AND input_hash = :input_hash"
            ),
            {"session_id": session_id, "input_hash": values["input_hash"]},
        )
        return existing.scalar_one()

    async def _run_manifest(
        self,
        *,
        session_row: dict[str, Any],
        plan: dict[str, Any],
        bundles: list[dict[str, Any]],
        assessment_id,
    ) -> dict[str, Any]:
        frozen_mappings = [
            mapping
            for entry in plan["kbd_ruleset_snapshot"]
            for mapping in entry.get("offline_signal_mappings", [])
            if isinstance(mapping, dict)
        ]
        mapping_payload = sorted(
            frozen_mappings,
            key=lambda item: (
                str(item.get("source_kbd_id")),
                str(item.get("source_kbd_revision")),
                str(item.get("source_signal_id")),
                str(item.get("collector_id")),
            ),
        )
        return {
            "schema_version": "1.0",
            "session": {
                "session_id": str(session_row["session_id"]),
                "version": session_row["version"],
                "selected_scenario": session_row["selected_scenario"],
            },
            "collection_profile": {
                "resource_id": plan["profile_name"],
                "revision": plan["profile_revision"],
                "version": plan["profile_version"],
                "checksum": plan["profile_checksum"],
            },
            "collection_plan": {
                "resource_id": str(plan["plan_id"]),
                "sequence": plan["plan_sequence"],
                "revision": plan["plan_revision"],
                "request_hash": plan["request_hash"],
            },
            "bundles": [
                {
                    "resource_id": str(bundle["bundle_id"]),
                    "sha256": bundle["sha256"],
                    "schema_version": bundle["schema_version"],
                    "type": bundle["bundle_type"],
                }
                for bundle in bundles
            ],
            "assessment": {"resource_id": str(assessment_id), "algorithm_version": ASSESSMENT_ALGORITHM_VERSION},
            "kbd_ruleset": {
                "checksum": plan["kbd_ruleset_checksum"],
                "entries": plan["kbd_ruleset_snapshot"],
            },
            "offline_signal_mapping": {
                "count": len(mapping_payload),
                "checksum": _canonical_hash(mapping_payload),
                "entries": mapping_payload,
            },
            "matcher": {"version": MATCHER_VERSION},
            "agent": {"version": AGENT_VERSION, "model_version": None},
            "conclusion_policy": {"version": CONCLUSION_POLICY_VERSION},
            "report_schema": {"version": REPORT_SCHEMA_VERSION},
        }

    async def _create_supplement(
        self,
        *,
        tenant_id: str,
        session_row: dict[str, Any],
        run_id,
        plan: dict[str, Any],
        plan_items: list[dict[str, Any]],
        evidence: list[dict[str, Any]],
        assessment: dict[str, Any],
        evaluations: list[dict[str, Any]],
        trace_id: str,
    ) -> uuid.UUID | None:
        missing_collectors = {item["collector_id"] for item in assessment["missing_evidence"]}
        unknown_collectors = set()
        for evaluation in evaluations:
            if evaluation["state"] == "UNKNOWN":
                matcher = evaluation.get("matcher_snapshot") or {}
                offline_mapping = matcher.get("_offline_mapping") or {}
                unknown_collectors.update(offline_mapping.get("collector_ids") or [])
        available_collectors = {item["collector_id"] for item in evidence if item["evidence_status"] == "available"}
        selected = [
            item
            for item in plan_items
            if item["collector_id"] not in available_collectors
            and (
                item["activation_state"] == "deferred"
                or item["collector_id"] in missing_collectors | unknown_collectors
            )
        ]
        if not selected:
            return None
        supplement_id = uuid.uuid4()
        items = [
            {
                "collector_id": item["collector_id"],
                "target": item["target"],
                "time_window": item["time_window"],
                "required_level": "mandatory",
                "reason": item["reason"],
            }
            for item in selected
        ]
        reason = "基础证据或关键 Signal 不足，需执行一次定向补充采集"
        await self._session.execute(
            text(
                """
                INSERT INTO supplement_plan (
                    supplement_plan_id, tenant_id, session_id, run_id, reason, collection_items,
                    expected_size_mb, expected_duration_minutes, trace_id
                ) VALUES (
                    :supplement_id, :tenant_id, :session_id, :run_id, :reason,
                    CAST(:items AS jsonb), :size_mb, :duration_minutes, :trace_id
                )
                ON CONFLICT (session_id) DO NOTHING
                """
            ),
            {
                "supplement_id": supplement_id,
                "tenant_id": tenant_id,
                "session_id": session_row["session_id"],
                "run_id": run_id,
                "reason": reason,
                "items": json.dumps(items, ensure_ascii=False),
                "size_mb": sum(float(item["expected_size_mb"]) for item in selected),
                "duration_minutes": math.ceil(sum(item["timeout_seconds"] for item in selected) / 60),
                "trace_id": trace_id,
            },
        )
        existing = await self._session.execute(
            text("SELECT supplement_plan_id FROM supplement_plan WHERE session_id = :session_id"),
            {"session_id": session_row["session_id"]},
        )
        supplement_id = existing.scalar_one()
        new_plan_id = uuid.uuid4()
        await self._session.execute(
            text(
                """
                INSERT INTO collection_plan (
                    plan_id, session_id, tenant_id, created_by, plan_sequence, plan_revision,
                    profile_name, profile_revision, profile_version, profile_checksum,
                    product_version, kbd_ruleset_snapshot, kbd_ruleset_checksum,
                    context_snapshot, required_permissions, sensitive_data_types,
                    unresolved_variables, estimated_size_mb, estimated_duration_seconds,
                    status, idempotency_key, request_hash, trace_id
                ) VALUES (
                    :plan_id, :session_id, :tenant_id, :created_by, 1, 1,
                    :profile_name, :profile_revision, :profile_version, :profile_checksum,
                    :product_version, CAST(:kbd_ruleset_snapshot AS jsonb), :kbd_ruleset_checksum,
                    CAST(:context_snapshot AS jsonb),
                    CAST(:required_permissions AS jsonb), CAST(:sensitive_data_types AS jsonb),
                    '[]'::jsonb, :size_mb, :duration_seconds,
                    'ready', :idempotency_key, :request_hash, :trace_id
                )
                ON CONFLICT (session_id, plan_sequence, plan_revision) DO NOTHING
                """
            ),
            {
                "plan_id": new_plan_id,
                "session_id": session_row["session_id"],
                "tenant_id": tenant_id,
                "created_by": "diagnosis-worker",
                "profile_name": plan["profile_name"],
                "profile_revision": plan["profile_revision"],
                "profile_version": plan["profile_version"],
                "profile_checksum": plan["profile_checksum"],
                "product_version": plan["product_version"],
                "kbd_ruleset_snapshot": json.dumps(plan["kbd_ruleset_snapshot"], ensure_ascii=False),
                "kbd_ruleset_checksum": plan["kbd_ruleset_checksum"],
                "context_snapshot": json.dumps(
                    {"supplement_plan_id": str(supplement_id), "parent_plan_id": str(plan["plan_id"])}
                ),
                "required_permissions": json.dumps(
                    sorted({permission for item in selected for permission in (item["required_permissions"] or [])})
                ),
                "sensitive_data_types": json.dumps(
                    sorted({value for item in selected for value in (item["sensitive_data_types"] or [])})
                ),
                "size_mb": sum(float(item["expected_size_mb"]) for item in selected),
                "duration_seconds": sum(item["timeout_seconds"] for item in selected),
                "idempotency_key": f"supplement:{session_row['session_id']}",
                "request_hash": _canonical_hash(items),
                "trace_id": trace_id,
            },
        )
        plan_result = await self._session.execute(
            text(
                """
                SELECT plan_id FROM collection_plan
                WHERE session_id = :session_id AND plan_sequence = 1 AND status = 'ready'
                ORDER BY plan_revision DESC LIMIT 1
                """
            ),
            {"session_id": session_row["session_id"]},
        )
        new_plan_id = plan_result.scalar_one()
        count_result = await self._session.execute(
            text("SELECT COUNT(*) FROM collection_plan_item WHERE plan_id = :plan_id"),
            {"plan_id": new_plan_id},
        )
        if count_result.scalar_one() == 0:
            for sequence, item in enumerate(selected, start=1):
                await self._session.execute(
                    text(
                        """
                        INSERT INTO collection_plan_item (
                            plan_id, sequence, collector_id, display_name, required_level,
                            activation_state, target, time_window, condition_snapshot, reason,
                            expected_size_mb, timeout_seconds, required_permissions,
                            sensitive_data_types, trace_id
                        ) VALUES (
                            :plan_id, :sequence, :collector_id, :display_name, 'mandatory',
                            'active', CAST(:target AS jsonb), CAST(:time_window AS jsonb),
                            CAST(:condition_snapshot AS jsonb), :reason, :expected_size_mb,
                            :timeout_seconds, CAST(:required_permissions AS jsonb),
                            CAST(:sensitive_data_types AS jsonb), :trace_id
                        )
                        """
                    ),
                    {
                        "plan_id": new_plan_id,
                        "sequence": sequence,
                        "collector_id": item["collector_id"],
                        "display_name": item["display_name"],
                        "target": json.dumps(item["target"]),
                        "time_window": json.dumps(item["time_window"]),
                        "condition_snapshot": json.dumps(item["condition_snapshot"]),
                        "reason": f"补采：{item['reason']}",
                        "expected_size_mb": item["expected_size_mb"],
                        "timeout_seconds": item["timeout_seconds"],
                        "required_permissions": json.dumps(item["required_permissions"]),
                        "sensitive_data_types": json.dumps(item["sensitive_data_types"]),
                        "trace_id": trace_id,
                    },
                )
        return supplement_id

    async def _create_report(
        self,
        *,
        tenant_id: str,
        session_row: dict[str, Any],
        run_id,
        run_sequence: int,
        candidates: list[dict[str, Any]],
        evaluations: list[dict[str, Any]],
        assessment: dict[str, Any],
        conclusion: dict[str, Any],
        supplement_id,
        trace_id: str,
    ) -> uuid.UUID:
        top = candidates[0] if candidates else None
        matched_refs = sorted(
            {
                ref
                for evaluation in evaluations
                if evaluation["state"] == "MATCHED"
                for ref in evaluation["evidence_refs"]
            }
        )
        counter_refs = sorted(
            {
                ref
                for evaluation in evaluations
                if evaluation["state"] == "NOT_MATCHED"
                for ref in evaluation["evidence_refs"]
            }
        )
        summary = _report_summary(conclusion["level"], top, assessment)
        report_id = uuid.uuid4()
        await self._session.execute(
            text(
                """
                INSERT INTO diagnosis_report (
                    report_id, tenant_id, session_id, run_id, report_sequence,
                    diagnosis_level, summary, resolved_domain, primary_hypothesis, confidence,
                    supporting_evidence, counter_evidence, excluded_causes, missing_evidence,
                    recommended_recovery, risk_and_rollback, root_cause_validation,
                    supplement_plan_id, matched_kbds, conclusion_policy_version,
                    report_schema_version, trace_id
                ) VALUES (
                    :report_id, :tenant_id, :session_id, :run_id, :report_sequence,
                    :level, :summary, :resolved_domain, :hypothesis, :confidence,
                    CAST(:supporting AS jsonb), CAST(:counter AS jsonb), CAST(:excluded AS jsonb),
                    CAST(:missing AS jsonb), CAST(:recovery AS jsonb), CAST(:risk AS jsonb),
                    CAST(:validation AS jsonb), :supplement_id, CAST(:matched_kbds AS jsonb),
                    :policy_version, :schema_version, :trace_id
                )
                """
            ),
            {
                "report_id": report_id,
                "tenant_id": tenant_id,
                "session_id": session_row["session_id"],
                "run_id": run_id,
                "report_sequence": run_sequence,
                "level": conclusion["level"],
                "summary": summary,
                "resolved_domain": top["category_id"] if top else session_row["selected_category"],
                "hypothesis": top["root_cause"] if top and conclusion["level"] != "Insufficient" else None,
                "confidence": conclusion["confidence"],
                "supporting": json.dumps([{"evidence_ref": ref} for ref in matched_refs]),
                "counter": json.dumps([{"evidence_ref": ref} for ref in counter_refs]),
                "excluded": json.dumps(
                    [{"support_id": item["support_id"], "title": item["title"]} for item in candidates[1:4]]
                ),
                "missing": json.dumps(assessment["missing_evidence"], ensure_ascii=False),
                "recovery": json.dumps(
                    [{"step": top["solution"], "source": f"KBD-{top['support_id']}"}]
                    if top and top["solution"]
                    else [],
                    ensure_ascii=False,
                ),
                "risk": json.dumps(
                    [{"risk": top["operational_impact"] or "执行前确认业务影响", "rollback": "按变更流程准备回退"}]
                    if top
                    else [],
                    ensure_ascii=False,
                ),
                "validation": json.dumps(
                    [{"step": top["recommendations"] or "复测故障场景并确认告警消失"}] if top else [],
                    ensure_ascii=False,
                ),
                "supplement_id": supplement_id,
                "matched_kbds": json.dumps(
                    [
                        {
                            "support_id": item["support_id"],
                            "title": item["title"],
                            "score": item["score"],
                            "category_id": item["category_id"],
                        }
                        for item in candidates
                    ],
                    ensure_ascii=False,
                ),
                "policy_version": CONCLUSION_POLICY_VERSION,
                "schema_version": REPORT_SCHEMA_VERSION,
                "trace_id": trace_id,
            },
        )
        return report_id

    async def _insert_evaluation(self, run_id, trace_id: str, item: dict[str, Any]) -> None:
        await self._session.execute(
            text(
                """
                INSERT INTO signal_evaluation (
                    run_id, signal_id, state, reason, required_for_conclusion,
                    evidence_status, evidence_refs, matcher_snapshot, trace_id
                ) VALUES (
                    :run_id, :signal_id, :state, :reason, :required,
                    :evidence_status, CAST(:evidence_refs AS jsonb),
                    CAST(:matcher_snapshot AS jsonb), :trace_id
                )
                ON CONFLICT (run_id, signal_id) DO NOTHING
                """
            ),
            {
                "run_id": run_id,
                "signal_id": item["signal_id"],
                "state": item["state"],
                "reason": item["reason"],
                "required": item["required_for_conclusion"],
                "evidence_status": item["evidence_status"],
                "evidence_refs": json.dumps(item["evidence_refs"]),
                "matcher_snapshot": json.dumps(item["matcher_snapshot"]),
                "trace_id": trace_id,
            },
        )

    async def _insert_candidate(self, run_id, trace_id: str, item: dict[str, Any]) -> None:
        await self._session.execute(
            text(
                """
                INSERT INTO diagnosis_candidate (
                    run_id, kbd_id, support_id, title, category_id, score,
                    matched_count, not_matched_count, unknown_count, signal_coverage,
                    kbd_snapshot, trace_id
                ) VALUES (
                    :run_id, :kbd_id, :support_id, :title, :category_id, :score,
                    :matched_count, :not_matched_count, :unknown_count, :signal_coverage,
                    CAST(:snapshot AS jsonb), :trace_id
                )
                """
            ),
            {
                "run_id": run_id,
                "trace_id": trace_id,
                **{
                    key: value
                    for key, value in item.items()
                    if key
                    in {
                        "kbd_id",
                        "support_id",
                        "title",
                        "category_id",
                        "score",
                        "matched_count",
                        "not_matched_count",
                        "unknown_count",
                        "signal_coverage",
                    }
                },
                "snapshot": json.dumps(item["snapshot"], ensure_ascii=False),
            },
        )

    async def _lock_session(self, tenant_id: str, session_id: str) -> dict[str, Any]:
        result = await self._session.execute(
            text(
                "SELECT * FROM diagnosis_session WHERE tenant_id = :tenant_id AND session_id = :session_id FOR UPDATE"
            ),
            {"tenant_id": tenant_id, "session_id": session_id},
        )
        row = result.mappings().one_or_none()
        if row is None:
            raise DiagnosisError(code="DIAGNOSIS_SESSION_NOT_FOUND", message="诊断会话不存在", http_status=404)
        return dict(row)

    async def _ready_bundles(self, tenant_id: str, session_id: str) -> list[dict[str, Any]]:
        result = await self._session.execute(
            text(
                """
                SELECT * FROM diagnostic_evidence_bundle
                WHERE tenant_id = :tenant_id AND session_id = :session_id
                  AND processing_status = 'ready'
                ORDER BY created_at, bundle_id
                """
            ),
            {"tenant_id": tenant_id, "session_id": session_id},
        )
        return [dict(row) for row in result.mappings().all()]

    async def _load_current_plan(self, session_id: str) -> dict[str, Any]:
        result = await self._session.execute(
            text(
                """
                SELECT * FROM collection_plan
                WHERE session_id = :session_id
                  AND status = 'ready'
                ORDER BY plan_sequence DESC, plan_revision DESC LIMIT 1
                """
            ),
            {"session_id": session_id},
        )
        return dict(result.mappings().one())

    async def _load_plan_items(self, plan_id) -> list[dict[str, Any]]:
        result = await self._session.execute(
            text("SELECT * FROM collection_plan_item WHERE plan_id = :plan_id ORDER BY sequence"),
            {"plan_id": plan_id},
        )
        return [dict(row) for row in result.mappings().all()]

    async def _load_evidence(self, tenant_id: str, session_id: str) -> list[dict[str, Any]]:
        result = await self._session.execute(
            text(
                """
                SELECT * FROM evidence_item
                WHERE tenant_id = :tenant_id AND session_id = :session_id
                ORDER BY created_at, evidence_id
                """
            ),
            {"tenant_id": tenant_id, "session_id": session_id},
        )
        return [dict(row) for row in result.mappings().all()]

    async def _get_run(self, run_id, tenant_id: str) -> dict[str, Any]:
        result = await self._session.execute(
            text("SELECT * FROM diagnosis_run WHERE run_id = :run_id AND tenant_id = :tenant_id"),
            {"run_id": run_id, "tenant_id": tenant_id},
        )
        return dict(result.mappings().one())

    @staticmethod
    def _require_read_role(actor: ActorContext) -> None:
        if actor.roles.isdisjoint(REPORT_READ_ROLES):
            raise DiagnosisError(code="FORBIDDEN", message="当前角色无权读取诊断结果", http_status=403)


def _to_shared_matcher(matcher: Any) -> dict[str, Any] | None:
    """将离线历史 matcher 形状翻译为 v2 matcher dict，供共享 evaluate_matcher 使用。

    历史形状（仅 4 种类型，无 extract）：
      keyword / not_keyword / equals / not_equals / numeric

    翻译规则：
      - keyword → {type: keyword, mode: or, expected: True,
                     extract: {type: text, rows: {mode: all}, cardinality: all}}
      - not_keyword → 同上，mode: not
      - equals → {type: state, pattern: [value], expected: True,
                   extract: {type: json, path, cardinality: exactly_one}}
      - not_equals → 同上，expected: False
      - numeric → {type: threshold, operator, value, expected: True,
                    extract: {type: json, path, value_mode: number, cardinality: exactly_one}}
      - 已有 extract 字段（v2）→ 原样返回
    """

    if not isinstance(matcher, dict):
        return None
    if matcher.get("extract") is not None:
        return matcher
    matcher_type = matcher.get("type")
    if matcher_type == "keyword":
        return {
            "type": "keyword",
            "mode": "or",
            "expected": True,
            "extract": {"type": "text", "rows": {"mode": "all"}, "cardinality": "all"},
            "pattern": matcher.get("pattern"),
        }
    if matcher_type == "not_keyword":
        return {
            "type": "keyword",
            "mode": "not",
            "expected": True,
            "extract": {"type": "text", "rows": {"mode": "all"}, "cardinality": "all"},
            "pattern": matcher.get("pattern"),
        }
    if matcher_type == "equals":
        return {
            "type": "state",
            "expected": True,
            "extract": {
                "type": "json",
                "path": str(matcher.get("path") or ""),
                "cardinality": "exactly_one",
                "value_mode": "string",
            },
            "pattern": [str(matcher.get("value") or "")],
        }
    if matcher_type == "not_equals":
        return {
            "type": "state",
            "expected": False,
            "extract": {
                "type": "json",
                "path": str(matcher.get("path") or ""),
                "cardinality": "exactly_one",
                "value_mode": "string",
            },
            "pattern": [str(matcher.get("value") or "")],
        }
    if matcher_type == "numeric":
        op_map = {"gt": ">", "gte": ">=", "lt": "<", "lte": "<=", "eq": "="}
        return {
            "type": "threshold",
            "expected": True,
            "extract": {
                "type": "json",
                "path": str(matcher.get("path") or ""),
                "cardinality": "exactly_one",
                "value_mode": "number",
            },
            "operator": op_map.get(str(matcher.get("operator") or ""), ">"),
            "value": matcher.get("value"),
        }
    return None


def _evaluate_matcher(matcher: Any, value: Any) -> bool | None:
    """委托共享 evaluate_matcher 做确定性求值，覆盖全部 7 种 matcher 类型。

    将离线历史形状翻译为 v2 后调用共享实现，保持与在线模式一致的判定语义。
    """

    from shared.signals.matcher import evaluate_matcher

    translated = _to_shared_matcher(matcher)
    if translated is None:
        return None
    text_value = _flatten_text(value)
    result = evaluate_matcher(translated, text_value)
    return result.matched


def row_to_kbd_dict(row: dict[str, Any]) -> dict[str, Any]:
    """将 kbd_entry 数据库行转换为 kbd_from_dict 可接受的字典。"""

    signals_document = row.get("signals_json") or {}
    signals = signals_document.get("signals", []) if isinstance(signals_document, dict) else []
    return {
        "id": row.get("id"),
        "support_id": row.get("support_id", ""),
        "name": row.get("title", ""),
        "category_id": row.get("category_id", ""),
        "root_cause": row.get("root_cause", ""),
        "solution": row.get("solution", ""),
        "signals": signals if isinstance(signals, list) else [],
        "verification_contract": signals_document.get("verification_contract", {})
        if isinstance(signals_document, dict)
        else {},
        "generation_metadata": signals_document.get("generation_metadata", {})
        if isinstance(signals_document, dict)
        else {},
        "publish_validation": signals_document.get("publish_validation", {})
        if isinstance(signals_document, dict)
        else {},
        "metadata": row.get("metadata") or {},
        "resource_revision": {
            "revision": row.get("resource_revision") or 0,
            "checksum": row.get("resource_checksum"),
        },
    }


def _conclusion(
    assessment: dict[str, Any],
    candidates: list[dict[str, Any]],
    cdd_decision: Any = None,
) -> dict[str, Any]:
    """版本化结论策略；优先使用 CDD 门禁结论，回退到旧启发式。"""

    if not assessment["ready_for_diagnosis"] or not candidates:
        return {"level": "Insufficient", "confidence": min(0.39, assessment["completeness_score"] / 100)}

    # 优先使用 CDD 结论门禁的判定
    if cdd_decision is not None:
        from shared.cdd import ConclusionLevel

        cdd_level = cdd_decision.level
        supported_ids = tuple(getattr(cdd_decision, "supported_ids", ()) or ())
        mandatory_complete = assessment["mandatory_total"] > 0 and (
            assessment["mandatory_available"] == assessment["mandatory_total"]
        )
        unique_supported = len(supported_ids) == 1
        top = candidates[0]
        top_is_supported = top.get("cdd_state") == "SUPPORTED" and str(top.get("kbd_id")) in supported_ids
        top_is_complete = top.get("unknown_count", 0) == 0 and top.get("not_matched_count", 0) == 0
        if (
            cdd_level == ConclusionLevel.DEFINITIVE
            and mandatory_complete
            and unique_supported
            and top_is_supported
            and top_is_complete
        ):
            return {"level": "Confirmed", "confidence": 0.95, "cdd_level": cdd_level.value}
        elif cdd_level in {ConclusionLevel.DEFINITIVE, ConclusionLevel.PARTIAL}:
            return {"level": "Probable", "confidence": round(top["score"], 4), "cdd_level": cdd_level.value}
        elif cdd_level == ConclusionLevel.INCONCLUSIVE:
            top = candidates[0]
            if top.get("matched_count", 0) > 0:
                return {"level": "Suspected", "confidence": round(top["score"], 4), "cdd_level": cdd_level.value}
            return {
                "level": "Insufficient",
                "confidence": min(0.39, assessment["completeness_score"] / 100),
                "cdd_level": cdd_level.value,
            }
        elif cdd_level == ConclusionLevel.NO_MATCH:
            return {
                "level": "Insufficient",
                "confidence": min(0.3, assessment["completeness_score"] / 100),
                "cdd_level": cdd_level.value,
            }

    # 回退：旧启发式
    top = candidates[0]
    second_score = candidates[1]["score"] if len(candidates) > 1 else 0.0
    if top["matched_count"] > 0 and top["not_matched_count"] > 0:
        return {"level": "Conflicted", "confidence": round(top["score"], 4)}
    if (
        top["score"] >= 0.85
        and top["unknown_count"] == 0
        and top["not_matched_count"] == 0
        and assessment["mandatory_available"] == assessment["mandatory_total"]
        and top["score"] - second_score >= 0.25
    ):
        return {"level": "Confirmed", "confidence": round(top["score"], 4)}
    if top["score"] >= 0.6 and top["matched_count"] > 0:
        return {"level": "Probable", "confidence": round(top["score"], 4)}
    return {"level": "Suspected", "confidence": round(max(0.4, top["score"]), 4)}


def _report_summary(level: str, top: dict[str, Any] | None, assessment: dict[str, Any]) -> str:
    if level == "Insufficient":
        return f"当前证据完整度 {assessment['completeness_score']}%，不足以形成可靠根因结论。"
    if top is None:
        return "未检索到可执行的 KBD（知识诊断文档）候选。"
    suffix = "仍需补充或人工确认缺失证据。" if level in {"Probable", "Suspected"} else ""
    if level == "Conflicted":
        suffix = "现有可信证据存在冲突，必须由领域专家复核。"
    return (
        f"{level}：候选案例 {top['support_id']}（{top['title']}），Signal 覆盖率 {top['signal_coverage']:.0%}。{suffix}"
    )


def _source_matches(target: dict[str, Any], source: dict[str, Any]) -> bool:
    target_id = target.get("source_node") or target.get("id")
    source_id = source.get("source_node") or source.get("id")
    return not target_id or target_id in {"diagnosis_session", "source_node"} or target_id == source_id


def _missing_evidence_metadata(matches: list[dict[str, Any]]) -> dict[str, Any]:
    """从不可变 Evidence Item（证据项）提取有界、可展示的失败原因。"""

    statuses = {str(item.get("evidence_status") or "") for item in matches}
    failure_reasons = sorted({str(item["failure_reason"]) for item in matches if item.get("failure_reason")})
    failure_details: list[str] = []
    for item in matches:
        structured = item.get("structured_data")
        if not isinstance(structured, dict):
            continue
        preview = structured.get("preview")
        source_path = str(item.get("source_path") or "")
        if isinstance(preview, str) and preview.strip() and source_path.endswith(".stderr"):
            detail = _ANSI_ESCAPE_RE.sub("", str(redact_observation_value(preview.strip())))[:500]
            if detail not in failure_details:
                failure_details.append(detail)
    combined_details = "\n".join(failure_details)
    if "当前命令仅支持" in combined_details and "版本" in combined_details:
        failure_reasons.insert(0, "collector_product_version_unsupported")
    elif "Additional property --formatter is not allowed" in combined_details:
        failure_reasons.insert(0, "collector_argument_unsupported")
    failure_reasons = list(dict.fromkeys(failure_reasons))
    if "available" in statuses:
        return {
            "status": "assessment_link_mismatch",
            "failure_reasons": ["historical_assessment_link_mismatch"],
            "failure_details": ["证据已采集成功，但历史评估未正确关联；新评估已由 completeness-v3 修复。"],
        }
    status = OfflineEvidenceProvider._missing_status(statuses)
    return {
        "status": status,
        "failure_reasons": failure_reasons,
        "failure_details": failure_details,
    }


def _resolve_json_path(value: Any, path: str) -> Any | None:
    if not path:
        return value
    current = value
    for part in path.removeprefix("$.").split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return None
    return current


def _flatten_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dict) and isinstance(value.get("preview"), str):
        return value["preview"]
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _numbers(value: Any) -> list[float]:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return [float(value)]
    if isinstance(value, list):
        return [float(item) for item in value if isinstance(item, (int, float)) and not isinstance(item, bool)]
    return []


def _canonical_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def _report_snapshot(report: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "report_id",
        "diagnosis_level",
        "summary",
        "recommended_recovery",
        "publish_status",
        "version",
    )
    return {key: report[key] for key in keys}
