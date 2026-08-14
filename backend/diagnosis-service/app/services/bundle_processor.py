"""隔离 Worker 的证据包安全处理与标准化流水线。"""

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import anyio
from shared.observability.logger import get_logger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.domain.evidence_bundle import SafeBundleExtractor, _clear_directory, bounded_structured_data
from app.errors import DiagnosisError
from app.services.envelope_encryption import ENCRYPTED_MAGICS, MAGIC, EnvelopeEncryptionService
from app.services.object_storage import LocalObjectStorage
from app.services.offline_analysis_service import OfflineAnalysisService

logger = get_logger("bundle-processor")

STATUS_MAP = {
    "success": "available",
    "partial": "available",
    "failed": "collection_failed",
    "not_applicable": "not_applicable",
    "skipped_by_user": "skipped_by_user",
    "out_of_time_range": "out_of_time_range",
}


class BundleProcessor:
    """处理单个 Bundle（证据包），所有明文只存在于独立临时目录。"""

    def __init__(
        self,
        *,
        session: AsyncSession,
        storage: LocalObjectStorage,
        encryption: EnvelopeEncryptionService | None,
        worker_id: str,
    ):
        self._session = session
        self._storage = storage
        self._encryption = encryption
        self._worker_id = worker_id

    async def process(self, *, task_id: str) -> None:
        """校验、解密、扫描、解压、标准化、评估并生成诊断结果。"""

        job = await self._claim_job(task_id)
        if job is None:
            return
        bundle = await self._load_bundle(job)
        await self._session.commit()
        object_path = self._storage.object_path(bundle["object_key"])
        work_parent = self._storage.work_root / f"{job['task_id']}-{job['attempts']}-{uuid.uuid4().hex[:8]}"
        archive_path = object_path
        encryption_metadata: dict[str, Any] = {}
        try:
            work_parent.mkdir(parents=True, exist_ok=False, mode=0o700)
            await self._set_bundle_status(bundle["bundle_id"], "quarantined")
            await self._session.commit()
            with object_path.open("rb") as source:
                magic = source.read(len(MAGIC))
            if magic in ENCRYPTED_MAGICS:
                if self._encryption is None:
                    raise DiagnosisError(
                        code="ENCRYPTION_PROVIDER_UNAVAILABLE",
                        message="证据包已加密，但隔离 Worker 未配置解密密钥",
                        http_status=503,
                    )
                archive_path = work_parent / "bundle.tar.gz"
                encryption_metadata = await anyio.to_thread.run_sync(
                    self._encryption.decrypt_file,
                    object_path,
                    archive_path,
                )
            elif bundle["bundle_type"] != "verification":
                raise DiagnosisError(
                    code="BUNDLE_ENCRYPTION_REQUIRED",
                    message="正式诊断证据包必须使用 Envelope Encryption（信封加密）",
                    http_status=422,
                )
            await self._set_bundle_status(bundle["bundle_id"], "scanning")
            await self._session.commit()
            extractor = SafeBundleExtractor(
                max_files=settings.DIAGNOSIS_MAX_FILE_COUNT,
                max_file_bytes=settings.DIAGNOSIS_MAX_FILE_BYTES,
                max_extracted_bytes=settings.DIAGNOSIS_MAX_EXTRACTED_BYTES,
            )
            extracted_dir = work_parent / "extracted"
            await self._set_bundle_status(bundle["bundle_id"], "extracting")
            await self._session.commit()
            with anyio.fail_after(settings.DIAGNOSIS_WORKER_TIMEOUT_SECONDS):
                extracted = await anyio.to_thread.run_sync(extractor.extract, archive_path, extracted_dir)
            await self._validate_manifest(bundle, extracted.manifest)
            await self._session.commit()
            await self._set_bundle_status(bundle["bundle_id"], "assessing")
            await self._session.commit()
            await self._persist_evidence(bundle, extracted)
            await self._session.execute(
                text(
                    """
                    UPDATE diagnostic_evidence_bundle
                    SET schema_version = :schema_version,
                        manifest_json = CAST(:manifest AS jsonb),
                        encryption_metadata = CAST(:encryption AS jsonb),
                        security_results = CAST(:security_results AS jsonb),
                        processing_status = 'ready',
                        failure_code = NULL, failure_message = NULL,
                        version = version + 1, updated_at = CURRENT_TIMESTAMP
                    WHERE bundle_id = :bundle_id
                    """
                ),
                {
                    "bundle_id": bundle["bundle_id"],
                    "schema_version": extracted.manifest.schema_version,
                    "manifest": extracted.manifest.model_dump_json(),
                    "encryption": json.dumps(encryption_metadata, sort_keys=True),
                    "security_results": json.dumps(extracted.security_results, sort_keys=True),
                },
            )
            await self._session.commit()
            await OfflineAnalysisService(self._session).assess_and_diagnose(
                tenant_id=bundle["tenant_id"],
                session_id=str(bundle["session_id"]),
                trace_id=job["trace_id"],
            )
            await self._finish_job(job["task_id"], "succeeded")
            await self._session.commit()
        except DiagnosisError as exc:
            await self._session.rollback()
            await self._reject_or_fail(job, bundle, exc)
            await self._session.commit()
        except Exception as exc:
            await self._session.rollback()
            logger.error(
                event="bundle_processing_unexpected_error",
                bundle_id=str(bundle["bundle_id"]),
                task_id=str(job["task_id"]),
                trace_id=job["trace_id"],
                error_type=type(exc).__name__,
                error=str(exc),
                exc_info=True,
            )
            wrapped = DiagnosisError(
                code="BUNDLE_PROCESSING_FAILED",
                message="诊断证据包处理失败",
                http_status=500,
                retryable=True,
                details={"error_type": type(exc).__name__},
            )
            await self._reject_or_fail(job, bundle, wrapped)
            await self._session.commit()
        finally:
            if work_parent.exists():
                await anyio.to_thread.run_sync(_clear_directory, work_parent)

    async def _claim_job(self, task_id: str) -> dict[str, Any] | None:
        result = await self._session.execute(
            text(
                """
                UPDATE diagnosis_processing_job
                SET status = 'running', attempts = attempts + 1,
                    locked_by = :worker_id, locked_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                WHERE task_id = :task_id
                  AND status IN ('pending', 'failed')
                  AND attempts < max_attempts
                RETURNING *
                """
            ),
            {"task_id": task_id, "worker_id": self._worker_id},
        )
        row = result.mappings().one_or_none()
        return dict(row) if row else None

    async def _load_bundle(self, job: dict[str, Any]) -> dict[str, Any]:
        result = await self._session.execute(
            text(
                """
                SELECT b.*, s.case_id, s.selected_scenario, s.incident_start_time, s.incident_end_time
                FROM diagnostic_evidence_bundle b
                JOIN diagnosis_session s ON s.session_id = b.session_id
                WHERE b.bundle_id = :bundle_id AND b.tenant_id = :tenant_id
                FOR UPDATE OF b
                """
            ),
            {"bundle_id": job["bundle_id"], "tenant_id": job["tenant_id"]},
        )
        return dict(result.mappings().one())

    async def _validate_manifest(self, bundle: dict[str, Any], manifest) -> None:
        """绑定数据库权威上下文，拒绝包内标识替换。"""

        expected = {
            "case_id": bundle["case_id"],
            "session_id": str(bundle["session_id"]),
            "bundle_type": bundle["bundle_type"],
            "collection_plan_id": str(bundle["collection_plan_id"]),
        }
        actual = {
            "case_id": manifest.case_id,
            "session_id": manifest.session_id,
            "bundle_type": manifest.bundle_type,
            "collection_plan_id": manifest.collection_plan_id,
        }
        mismatches = {
            key: {"expected": expected[key], "actual": actual[key]} for key in expected if expected[key] != actual[key]
        }
        if mismatches:
            raise DiagnosisError(
                code="BUNDLE_CONTEXT_MISMATCH",
                message="manifest 与上传会话权威上下文不一致",
                http_status=422,
                details=mismatches,
            )
        parent = str(bundle["parent_bundle_id"]) if bundle["parent_bundle_id"] else None
        if manifest.parent_bundle_id != parent:
            raise DiagnosisError(code="BUNDLE_PARENT_MISMATCH", message="manifest 父包引用不一致", http_status=422)
        artifact_result = await self._session.execute(
            text(
                """
                SELECT artifact_sha256, signing_key_id, status, expires_at
                FROM collector_artifact WHERE artifact_id = :artifact_id
                """
            ),
            {"artifact_id": bundle["collector_artifact_id"]},
        )
        artifact = artifact_result.mappings().one()
        if (
            manifest.collector_artifact_sha256 != artifact["artifact_sha256"]
            or manifest.signature_key_id != artifact["signing_key_id"]
            or artifact["status"] != "ready"
            or artifact["expires_at"] <= datetime.now(UTC)
        ):
            raise DiagnosisError(
                code="BUNDLE_ARTIFACT_UNTRUSTED",
                message="manifest 引用的采集器制品不可信、已过期或已撤销",
                http_status=422,
            )

    async def _persist_evidence(self, bundle: dict[str, Any], extracted) -> None:
        plan_items_result = await self._session.execute(
            text(
                """
                SELECT item_id, collector_id, target
                FROM collection_plan_item
                WHERE plan_id = :plan_id
                ORDER BY sequence
                """
            ),
            {"plan_id": bundle["collection_plan_id"]},
        )
        plan_items = [dict(row) for row in plan_items_result.mappings().all()]
        by_collector: dict[str, list[dict[str, Any]]] = {}
        for item in plan_items:
            by_collector.setdefault(item["collector_id"], []).append(item)
        for collected in extracted.manifest.collection_items:
            candidates = by_collector.get(collected.collector_id, [])
            plan_item = next(
                (item for item in candidates if _source_matches_manifest(item["target"] or {}, collected.source)),
                candidates[0] if len(candidates) == 1 else None,
            )
            if plan_item is None:
                raise DiagnosisError(
                    code="BUNDLE_COLLECTOR_NOT_IN_PLAN",
                    message="manifest 包含采集计划之外的 Collector",
                    http_status=422,
                    details={"collector_id": collected.collector_id, "source": collected.source},
                )
            source_object = {"id": collected.source, "source_node": collected.source}
            if collected.files:
                for file in collected.files:
                    path = extracted.work_dir.joinpath(*Path(file.path).parts)
                    structured = await anyio.to_thread.run_sync(bounded_structured_data, path, file.media_type)
                    await self._insert_evidence(
                        bundle=bundle,
                        plan_item=plan_item,
                        collected=collected,
                        source_object=source_object,
                        source_path=file.path,
                        media_type=file.media_type,
                        sensitivity=file.sensitivity,
                        size_bytes=file.size_bytes,
                        sha256=file.sha256,
                        structured=structured,
                        status=STATUS_MAP[collected.status],
                    )
                    # 单个证据项即一笔短事务；Bundle 在 ready 前不会进入诊断输入集合。
                    await self._session.commit()
            else:
                synthetic_path = f"states/{collected.collector_id}/{collected.source}.status.json"
                await self._insert_evidence(
                    bundle=bundle,
                    plan_item=plan_item,
                    collected=collected,
                    source_object=source_object,
                    source_path=synthetic_path,
                    media_type="application/json",
                    sensitivity="internal",
                    size_bytes=0,
                    sha256="0" * 64,
                    structured={"status": collected.status, "failure_reason": collected.failure_reason},
                    status=STATUS_MAP[collected.status],
                )
                await self._session.commit()

    async def _insert_evidence(
        self,
        *,
        bundle: dict[str, Any],
        plan_item: dict[str, Any],
        collected,
        source_object: dict[str, Any],
        source_path: str,
        media_type: str,
        sensitivity: str,
        size_bytes: int,
        sha256: str,
        structured: Any,
        status: str,
    ) -> None:
        quality = "high" if status == "available" and collected.status == "success" else "low"
        await self._session.execute(
            text(
                """
                INSERT INTO evidence_item (
                    tenant_id, session_id, bundle_id, collection_plan_item_id, collector_id,
                    source_path, source_object, evidence_status, media_type, sensitivity,
                    collected_start, collected_end, source_timezone, clock_offset_ms,
                    size_bytes, sha256, object_ref, structured_data, quality,
                    failure_reason, trace_id
                ) VALUES (
                    :tenant_id, :session_id, :bundle_id, :plan_item_id, :collector_id,
                    :source_path, CAST(:source_object AS jsonb), :status, :media_type, :sensitivity,
                    :collected_start, :collected_end, :source_timezone, :clock_offset_ms,
                    :size_bytes, :sha256, :object_ref, CAST(:structured AS jsonb), :quality,
                    :failure_reason, :trace_id
                )
                ON CONFLICT (bundle_id, source_path) DO NOTHING
                """
            ),
            {
                "tenant_id": bundle["tenant_id"],
                "session_id": bundle["session_id"],
                "bundle_id": bundle["bundle_id"],
                "plan_item_id": plan_item["item_id"],
                "collector_id": collected.collector_id,
                "source_path": source_path,
                "source_object": json.dumps(source_object),
                "status": status,
                "media_type": media_type,
                "sensitivity": sensitivity,
                "collected_start": collected.time_coverage.start if collected.time_coverage else None,
                "collected_end": collected.time_coverage.end if collected.time_coverage else None,
                "source_timezone": collected.source_timezone,
                "clock_offset_ms": collected.clock_offset_ms,
                "size_bytes": size_bytes,
                "sha256": sha256,
                "object_ref": f"{bundle['object_key']}#{source_path}",
                "structured": json.dumps(structured, ensure_ascii=False),
                "quality": quality,
                "failure_reason": collected.failure_reason,
                "trace_id": bundle["trace_id"],
            },
        )

    async def _set_bundle_status(self, bundle_id, status: str) -> None:
        await self._session.execute(
            text(
                """
                UPDATE diagnostic_evidence_bundle
                SET processing_status = :status, version = version + 1, updated_at = CURRENT_TIMESTAMP
                WHERE bundle_id = :bundle_id
                """
            ),
            {"bundle_id": bundle_id, "status": status},
        )

    async def _finish_job(self, task_id, status: str, code: str | None = None, message: str | None = None) -> None:
        await self._session.execute(
            text(
                """
                UPDATE diagnosis_processing_job
                SET status = :status, failure_code = :code, failure_message = :message,
                    locked_by = NULL, locked_at = NULL, updated_at = CURRENT_TIMESTAMP
                WHERE task_id = :task_id
                """
            ),
            {"task_id": task_id, "status": status, "code": code, "message": message},
        )

    async def _reject_or_fail(self, job: dict[str, Any], bundle: dict[str, Any], exc: DiagnosisError) -> None:
        permanent = 400 <= exc.http_status < 500
        bundle_status = "rejected" if permanent else "failed"
        await self._session.execute(
            text(
                """
                UPDATE diagnostic_evidence_bundle
                SET processing_status = :status, failure_code = :code,
                    failure_message = :message, version = version + 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE bundle_id = :bundle_id
                """
            ),
            {
                "bundle_id": bundle["bundle_id"],
                "status": bundle_status,
                "code": exc.code,
                "message": exc.message,
            },
        )
        retryable = not permanent and job["attempts"] < job["max_attempts"]
        await self._finish_job(
            job["task_id"],
            "pending" if retryable else "failed",
            exc.code,
            exc.message,
        )


def _source_matches_manifest(target: dict[str, Any], source: str) -> bool:
    expected = target.get("source_node") or target.get("id")
    return not expected or expected in {"diagnosis_session", "source_node"} or expected == source
