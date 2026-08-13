"""诊断证据包直传会话与完成确认服务。"""

import hashlib
import json
import math
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from shared.observability.otel import get_current_trace_id
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import ActorContext
from app.config import settings
from app.errors import DiagnosisError
from app.schemas.evidence_lifecycle import UploadSessionCreate
from app.services.object_storage import LocalObjectStorage

UPLOAD_ROLES = frozenset(
    {"customer_admin", "field_engineer", "support_engineer", "domain_expert", "platform_admin", "diagnosis_worker"}
)


@dataclass(frozen=True, slots=True)
class UploadCreateResult:
    """创建上传会话的结果，明文 token 只在首次创建时返回。"""

    row: dict[str, Any]
    token: str | None
    created: bool


class EvidenceUploadService:
    """管理分片直传和证据包入隔离区。"""

    def __init__(self, *, session: AsyncSession, storage: LocalObjectStorage):
        self._session = session
        self._storage = storage

    async def create(
        self,
        *,
        actor: ActorContext,
        session_id: str,
        command: UploadSessionCreate,
        idempotency_key: str,
        source_ip: str | None,
    ) -> UploadCreateResult:
        """创建上传会话并签发一次性高熵直传令牌。"""

        self._require_role(actor)
        normalized_key = self._idempotency_key(idempotency_key)
        diagnosis_session = await self._lock_session(actor, session_id)
        await self._validate_references(actor, diagnosis_session, command)
        request_hash = self._request_hash(command)
        existing = await self._fetch_upload_by_idempotency(actor.tenant_id, normalized_key)
        if existing:
            if existing["request_hash"] != request_hash or str(existing["session_id"]) != session_id:
                raise DiagnosisError(
                    code="IDEMPOTENCY_KEY_REUSED",
                    message="同一 Idempotency-Key 已用于不同上传请求",
                    http_status=409,
                )
            return UploadCreateResult(existing, None, False)

        upload_id = uuid.uuid4()
        token = secrets.token_urlsafe(48)
        trace_id = get_current_trace_id() or secrets.token_hex(16)
        part_count = math.ceil(command.total_size_bytes / command.chunk_size_bytes)
        expires_at = datetime.now(UTC) + timedelta(seconds=settings.DIAGNOSIS_UPLOAD_TTL_SECONDS)
        object_key = f"quarantine/{actor.tenant_id}/{session_id}/{upload_id}/{command.file_name}"
        values = {
            "upload_id": upload_id,
            "session_id": session_id,
            "tenant_id": actor.tenant_id,
            "created_by": actor.user_id,
            "source_ip": source_ip,
            "bundle_type": command.bundle_type.value,
            "parent_bundle_id": command.parent_bundle_id,
            "collection_plan_id": command.collection_plan_id,
            "collector_artifact_id": command.collector_artifact_id,
            "file_name": command.file_name,
            "media_type": command.media_type,
            "total_size_bytes": command.total_size_bytes,
            "expected_sha256": command.sha256 or "0" * 64,
            "chunk_size_bytes": command.chunk_size_bytes,
            "part_count": part_count,
            "object_key": object_key,
            "upload_token_hash": hashlib.sha256(token.encode()).hexdigest(),
            "expires_at": expires_at,
            "idempotency_key": normalized_key,
            "request_hash": request_hash,
            "trace_id": trace_id,
        }
        await self._session.execute(
            text(
                """
                INSERT INTO diagnosis_upload_session (
                    upload_id, session_id, tenant_id, created_by, source_ip, bundle_type,
                    parent_bundle_id, collection_plan_id, collector_artifact_id, file_name,
                    media_type, total_size_bytes, expected_sha256, chunk_size_bytes, part_count,
                    object_key, upload_token_hash, expires_at, idempotency_key, request_hash, trace_id
                ) VALUES (
                    :upload_id, :session_id, :tenant_id, :created_by, CAST(:source_ip AS inet), :bundle_type,
                    :parent_bundle_id, :collection_plan_id, :collector_artifact_id, :file_name,
                    :media_type, :total_size_bytes, :expected_sha256, :chunk_size_bytes, :part_count,
                    :object_key, :upload_token_hash, :expires_at, :idempotency_key, :request_hash, :trace_id
                )
                """
            ),
            values,
        )
        diagnosis_session["status"] = str(diagnosis_session["status"])
        if diagnosis_session["status"] in {"plan_ready", "collecting"}:
            await self._session.execute(
                text(
                    """
                    UPDATE diagnosis_session
                    SET status = 'uploading', version = version + 1, updated_at = CURRENT_TIMESTAMP
                    WHERE session_id = :session_id
                    """
                ),
                {"session_id": session_id},
            )
        row = await self._fetch_upload(upload_id, actor.tenant_id)
        return UploadCreateResult(row, token, True)

    async def record_part(
        self,
        *,
        upload_id: str,
        part_number: int,
        token: str,
        chunks,
        claimed_sha256: str | None,
    ) -> dict[str, Any]:
        """验证直传令牌并流式保存分片。"""

        row = await self._fetch_upload(upload_id)
        self._assert_upload_token(row, token)
        self._assert_upload_active(row)
        if not 1 <= part_number <= row["part_count"]:
            raise DiagnosisError(code="INVALID_PART_NUMBER", message="分片编号超出范围", http_status=422)
        expected_size = min(
            row["chunk_size_bytes"],
            row["total_size_bytes"] - (part_number - 1) * row["chunk_size_bytes"],
        )
        # 文件数据面不持有数据库锁；最终用 JSONB 原子合并登记分片。
        await self._session.commit()
        size, sha256 = await self._storage.write_part(
            upload_id=upload_id,
            part_number=part_number,
            chunks=chunks,
            max_bytes=expected_size,
        )
        if size != expected_size:
            raise DiagnosisError(
                code="UPLOAD_PART_SIZE_MISMATCH",
                message="上传分片大小与会话声明不一致",
                http_status=422,
                details={"expected": expected_size, "actual": size},
            )
        if claimed_sha256 and claimed_sha256.lower() != sha256:
            raise DiagnosisError(code="UPLOAD_PART_HASH_MISMATCH", message="上传分片 SHA-256 不一致", http_status=422)
        part_metadata = json.dumps({"size_bytes": size, "sha256": sha256}, sort_keys=True)
        result = await self._session.execute(
            text(
                """
                UPDATE diagnosis_upload_session
                SET uploaded_parts = COALESCE(uploaded_parts, '{}'::jsonb)
                                     || jsonb_build_object(CAST(:part_key AS text), CAST(:part_metadata AS jsonb)),
                    status = 'uploading',
                    updated_at = CURRENT_TIMESTAMP
                WHERE upload_id = :upload_id
                  AND status IN ('initiated', 'uploading')
                  AND expires_at > CURRENT_TIMESTAMP
                RETURNING upload_id
                """
            ),
            {
                "upload_id": upload_id,
                "part_key": str(part_number),
                "part_metadata": part_metadata,
            },
        )
        if result.scalar_one_or_none() is None:
            raise DiagnosisError(code="UPLOAD_SESSION_NOT_ACTIVE", message="上传会话已过期或不可写", http_status=409)
        return {
            "upload_id": upload_id,
            "part_number": part_number,
            "size_bytes": size,
            "sha256": sha256,
            "status": "uploaded",
        }

    async def complete(
        self,
        *,
        actor: ActorContext,
        session_id: str,
        upload_id: str,
        part_numbers: list[int],
    ) -> tuple[dict[str, Any], bool]:
        """幂等完成上传，校验整体哈希后创建 Bundle 和异步任务。"""

        self._require_role(actor)
        row = await self._fetch_upload(upload_id, actor.tenant_id)
        if str(row["session_id"]) != session_id:
            raise DiagnosisError(code="UPLOAD_SESSION_NOT_FOUND", message="上传会话不存在", http_status=404)
        if row["status"] == "completed":
            bundle = await self._fetch_bundle_by_upload(upload_id, actor.tenant_id)
            return bundle, False
        self._assert_upload_active(row)
        expected_parts = list(range(1, row["part_count"] + 1))
        if sorted(part_numbers) != expected_parts:
            raise DiagnosisError(
                code="UPLOAD_PARTS_INCOMPLETE",
                message="上传分片列表不完整或顺序不合法",
                http_status=409,
                details={"expected": expected_parts, "actual": sorted(part_numbers)},
            )
        uploaded_parts = row["uploaded_parts"] or {}
        if any(str(part) not in uploaded_parts for part in expected_parts):
            raise DiagnosisError(code="UPLOAD_PARTS_INCOMPLETE", message="仍有分片未上传", http_status=409)

        claim = await self._session.execute(
            text(
                """
                UPDATE diagnosis_upload_session
                SET status = 'completing', updated_at = CURRENT_TIMESTAMP
                WHERE upload_id = :upload_id
                  AND tenant_id = :tenant_id
                  AND status IN ('initiated', 'uploading')
                  AND expires_at > CURRENT_TIMESTAMP
                RETURNING upload_id
                """
            ),
            {"upload_id": upload_id, "tenant_id": actor.tenant_id},
        )
        if claim.scalar_one_or_none() is None:
            raise DiagnosisError(code="UPLOAD_COMPLETION_IN_PROGRESS", message="上传完成操作已在执行", http_status=409)
        await self._session.commit()
        try:
            size, sha256 = await self._storage.complete_multipart(
                upload_id=upload_id,
                part_numbers=expected_parts,
                object_key=row["object_key"],
                max_bytes=settings.DIAGNOSIS_MAX_BUNDLE_BYTES,
            )
        except BaseException:
            await self._session.execute(
                text(
                    """
                    UPDATE diagnosis_upload_session
                    SET status = 'failed', updated_at = CURRENT_TIMESTAMP
                    WHERE upload_id = :upload_id AND status = 'completing'
                    """
                ),
                {"upload_id": upload_id},
            )
            await self._session.commit()
            raise
        hash_mismatch = row["expected_sha256"] != "0" * 64 and sha256 != row["expected_sha256"]
        if size != row["total_size_bytes"] or hash_mismatch:
            await self._session.execute(
                text(
                    """
                    UPDATE diagnosis_upload_session
                    SET status = 'failed', updated_at = CURRENT_TIMESTAMP
                    WHERE upload_id = :upload_id
                    """
                ),
                {"upload_id": upload_id},
            )
            await self._session.commit()
            await self._storage.delete_object(row["object_key"])
            raise DiagnosisError(
                code="BUNDLE_INTEGRITY_MISMATCH",
                message="整包大小或 SHA-256 与上传声明不一致",
                http_status=422,
                details={"expected_size": row["total_size_bytes"], "actual_size": size},
            )

        bundle_id = uuid.uuid4()
        retention_until = datetime.now(UTC) + timedelta(days=settings.DIAGNOSIS_BUNDLE_RETENTION_DAYS)
        try:
            result = await self._session.execute(
                text(
                    """
                    INSERT INTO diagnostic_evidence_bundle (
                        bundle_id, session_id, upload_id, tenant_id, uploaded_by, bundle_type,
                        parent_bundle_id, collection_plan_id, collector_artifact_id, object_key,
                        size_bytes, sha256, processing_status, retention_until, legal_hold, trace_id
                    ) VALUES (
                        :bundle_id, :session_id, :upload_id, :tenant_id, :uploaded_by, :bundle_type,
                        :parent_bundle_id, :collection_plan_id, :collector_artifact_id, :object_key,
                        :size_bytes, :sha256, 'uploaded', :retention_until,
                        (SELECT legal_hold FROM diagnosis_session WHERE session_id = :session_id),
                        :trace_id
                    )
                    ON CONFLICT (tenant_id, session_id, bundle_type, sha256) DO NOTHING
                    RETURNING bundle_id
                    """
                ),
                {
                    "bundle_id": bundle_id,
                    "session_id": row["session_id"],
                    "upload_id": row["upload_id"],
                    "tenant_id": row["tenant_id"],
                    "uploaded_by": row["created_by"],
                    "bundle_type": row["bundle_type"],
                    "parent_bundle_id": row["parent_bundle_id"],
                    "collection_plan_id": row["collection_plan_id"],
                    "collector_artifact_id": row["collector_artifact_id"],
                    "object_key": row["object_key"],
                    "size_bytes": size,
                    "sha256": sha256,
                    "retention_until": retention_until,
                    "trace_id": row["trace_id"],
                },
            )
            created_bundle_id = result.scalar_one_or_none()
        except Exception:
            await self._session.rollback()
            await self._session.execute(
                text("UPDATE diagnosis_upload_session SET status = 'failed' WHERE upload_id = :upload_id"),
                {"upload_id": upload_id},
            )
            await self._session.commit()
            raise

        if created_bundle_id is None:
            duplicate = await self._fetch_bundle_by_business_key(row, sha256)
            await self._session.execute(
                text(
                    """
                    UPDATE diagnosis_upload_session
                    SET status = 'completed', completed_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
                    WHERE upload_id = :upload_id
                    """
                ),
                {"upload_id": upload_id},
            )
            await self._storage.delete_multipart(upload_id)
            return duplicate, False

        await self._session.execute(
            text(
                """
                INSERT INTO diagnosis_processing_job (
                    tenant_id, session_id, bundle_id, trace_id
                ) VALUES (:tenant_id, :session_id, :bundle_id, :trace_id)
                ON CONFLICT (bundle_id, job_type) DO NOTHING
                """
            ),
            {
                "tenant_id": actor.tenant_id,
                "session_id": session_id,
                "bundle_id": created_bundle_id,
                "trace_id": row["trace_id"],
            },
        )
        await self._session.execute(
            text(
                """
                UPDATE diagnosis_upload_session
                SET status = 'completed', completed_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
                WHERE upload_id = :upload_id
                """
            ),
            {"upload_id": upload_id},
        )
        await self._session.execute(
            text(
                """
                UPDATE diagnosis_session
                SET status = 'assessing', version = version + 1, updated_at = CURRENT_TIMESTAMP
                WHERE session_id = :session_id
                  AND status IN ('uploading', 'collecting', 'supplement_required')
                """
            ),
            {"session_id": session_id},
        )
        if row["bundle_type"] == "supplement":
            await self._session.execute(
                text(
                    """
                    UPDATE supplement_plan
                    SET status = 'collecting', updated_at = CURRENT_TIMESTAMP
                    WHERE session_id = :session_id AND status = 'ready'
                    """
                ),
                {"session_id": session_id},
            )
        await self._storage.delete_multipart(upload_id)
        return await self._fetch_bundle(created_bundle_id, actor.tenant_id), True

    async def abort(self, *, actor: ActorContext, session_id: str, upload_id: str) -> dict[str, Any]:
        """显式终止未完成上传。"""

        self._require_role(actor)
        row = await self._fetch_upload_for_update(upload_id, actor.tenant_id)
        if str(row["session_id"]) != session_id:
            raise DiagnosisError(code="UPLOAD_SESSION_NOT_FOUND", message="上传会话不存在", http_status=404)
        if row["status"] == "completed":
            raise DiagnosisError(code="UPLOAD_ALREADY_COMPLETED", message="已完成上传不能终止", http_status=409)
        if row["status"] != "aborted":
            await self._session.execute(
                text("UPDATE diagnosis_upload_session SET status = 'aborted' WHERE upload_id = :upload_id"),
                {"upload_id": upload_id},
            )
            await self._storage.delete_multipart(upload_id)
        return {**row, "status": "aborted"}

    async def get_upload(self, *, actor: ActorContext, session_id: str, upload_id: str) -> dict[str, Any]:
        """读取上传会话。"""

        self._require_role(actor)
        row = await self._fetch_upload(upload_id, actor.tenant_id)
        if str(row["session_id"]) != session_id:
            raise DiagnosisError(code="UPLOAD_SESSION_NOT_FOUND", message="上传会话不存在", http_status=404)
        return row

    async def get_bundle(self, *, actor: ActorContext, session_id: str, bundle_id: str) -> dict[str, Any]:
        """读取本租户证据包。"""

        self._require_role(actor)
        row = await self._fetch_bundle(bundle_id, actor.tenant_id)
        if str(row["session_id"]) != session_id:
            raise DiagnosisError(code="EVIDENCE_BUNDLE_NOT_FOUND", message="诊断证据包不存在", http_status=404)
        return row

    async def list_bundles(self, *, actor: ActorContext, session_id: str) -> list[dict[str, Any]]:
        """按创建顺序读取会话证据包。"""

        self._require_role(actor)
        result = await self._session.execute(
            text(
                """
                SELECT * FROM diagnostic_evidence_bundle
                WHERE tenant_id = :tenant_id AND session_id = :session_id
                ORDER BY created_at, bundle_id
                """
            ),
            {"tenant_id": actor.tenant_id, "session_id": session_id},
        )
        return [dict(row) for row in result.mappings().all()]

    async def _lock_session(self, actor: ActorContext, session_id: str) -> dict[str, Any]:
        result = await self._session.execute(
            text(
                """
                SELECT * FROM diagnosis_session
                WHERE tenant_id = :tenant_id AND session_id = :session_id
                FOR UPDATE
                """
            ),
            {"tenant_id": actor.tenant_id, "session_id": session_id},
        )
        row = result.mappings().one_or_none()
        if row is None:
            raise DiagnosisError(code="DIAGNOSIS_SESSION_NOT_FOUND", message="诊断会话不存在", http_status=404)
        if str(row["status"]) not in {"plan_ready", "collecting", "uploading", "supplement_required"}:
            raise DiagnosisError(
                code="INVALID_SESSION_STATE",
                message="当前诊断会话状态不能创建上传",
                http_status=409,
                details={"status": str(row["status"])},
            )
        return dict(row)

    async def _validate_references(
        self, actor: ActorContext, diagnosis_session: dict[str, Any], command: UploadSessionCreate
    ) -> None:
        result = await self._session.execute(
            text(
                """
                SELECT p.plan_sequence, a.status AS artifact_status, a.expires_at
                FROM collection_plan p
                JOIN collector_artifact a
                  ON a.collection_plan_id = p.plan_id
                 AND a.session_id = p.session_id
                 AND a.tenant_id = p.tenant_id
                WHERE p.plan_id = :plan_id
                  AND a.artifact_id = :artifact_id
                  AND p.session_id = :session_id
                  AND p.tenant_id = :tenant_id
                """
            ),
            {
                "plan_id": command.collection_plan_id,
                "artifact_id": command.collector_artifact_id,
                "session_id": diagnosis_session["session_id"],
                "tenant_id": actor.tenant_id,
            },
        )
        reference = result.mappings().one_or_none()
        if reference is None:
            raise DiagnosisError(
                code="UPLOAD_REFERENCE_MISMATCH",
                message="采集计划或采集器制品不属于当前诊断会话",
                http_status=422,
            )
        if reference["artifact_status"] != "ready" or reference["expires_at"] <= datetime.now(UTC):
            raise DiagnosisError(code="COLLECTOR_ARTIFACT_UNTRUSTED", message="采集器制品已撤销或过期", http_status=409)
        expected_sequence = 1 if command.bundle_type.value == "supplement" else 0
        if reference["plan_sequence"] != expected_sequence:
            raise DiagnosisError(
                code="BUNDLE_PLAN_SEQUENCE_MISMATCH",
                message="证据包类型与采集计划轮次不一致",
                http_status=422,
            )
        if command.parent_bundle_id:
            parent_result = await self._session.execute(
                text(
                    """
                    SELECT bundle_type, processing_status
                    FROM diagnostic_evidence_bundle
                    WHERE bundle_id = :bundle_id AND session_id = :session_id AND tenant_id = :tenant_id
                    """
                ),
                {
                    "bundle_id": command.parent_bundle_id,
                    "session_id": diagnosis_session["session_id"],
                    "tenant_id": actor.tenant_id,
                },
            )
            parent = parent_result.mappings().one_or_none()
            if parent is None or parent["bundle_type"] != "initial" or parent["processing_status"] == "deleted":
                raise DiagnosisError(code="INVALID_PARENT_BUNDLE", message="补采父包无效", http_status=422)

    async def _fetch_upload(self, upload_id, tenant_id: str | None = None) -> dict[str, Any]:
        sql = "SELECT * FROM diagnosis_upload_session WHERE upload_id = :upload_id"
        params: dict[str, Any] = {"upload_id": upload_id}
        if tenant_id is not None:
            sql += " AND tenant_id = :tenant_id"
            params["tenant_id"] = tenant_id
        result = await self._session.execute(text(sql), params)
        row = result.mappings().one_or_none()
        if row is None:
            raise DiagnosisError(code="UPLOAD_SESSION_NOT_FOUND", message="上传会话不存在", http_status=404)
        return dict(row)

    async def _fetch_upload_for_update(self, upload_id, tenant_id: str | None = None) -> dict[str, Any]:
        sql = "SELECT * FROM diagnosis_upload_session WHERE upload_id = :upload_id"
        params: dict[str, Any] = {"upload_id": upload_id}
        if tenant_id is not None:
            sql += " AND tenant_id = :tenant_id"
            params["tenant_id"] = tenant_id
        sql += " FOR UPDATE"
        result = await self._session.execute(text(sql), params)
        row = result.mappings().one_or_none()
        if row is None:
            raise DiagnosisError(code="UPLOAD_SESSION_NOT_FOUND", message="上传会话不存在", http_status=404)
        return dict(row)

    async def _fetch_upload_by_idempotency(self, tenant_id: str, key: str) -> dict[str, Any] | None:
        result = await self._session.execute(
            text(
                """
                SELECT * FROM diagnosis_upload_session
                WHERE tenant_id = :tenant_id AND idempotency_key = :idempotency_key
                """
            ),
            {"tenant_id": tenant_id, "idempotency_key": key},
        )
        row = result.mappings().one_or_none()
        return dict(row) if row else None

    async def _fetch_bundle(self, bundle_id, tenant_id: str) -> dict[str, Any]:
        result = await self._session.execute(
            text(
                """
                SELECT * FROM diagnostic_evidence_bundle
                WHERE bundle_id = :bundle_id AND tenant_id = :tenant_id
                """
            ),
            {"bundle_id": bundle_id, "tenant_id": tenant_id},
        )
        row = result.mappings().one_or_none()
        if row is None:
            raise DiagnosisError(code="EVIDENCE_BUNDLE_NOT_FOUND", message="诊断证据包不存在", http_status=404)
        return dict(row)

    async def _fetch_bundle_by_upload(self, upload_id, tenant_id: str) -> dict[str, Any]:
        result = await self._session.execute(
            text(
                """
                SELECT * FROM diagnostic_evidence_bundle
                WHERE upload_id = :upload_id AND tenant_id = :tenant_id
                """
            ),
            {"upload_id": upload_id, "tenant_id": tenant_id},
        )
        row = result.mappings().one_or_none()
        if row is None:
            raise DiagnosisError(code="EVIDENCE_BUNDLE_NOT_FOUND", message="诊断证据包不存在", http_status=404)
        return dict(row)

    async def _fetch_bundle_by_business_key(self, upload: dict[str, Any], sha256: str) -> dict[str, Any]:
        result = await self._session.execute(
            text(
                """
                SELECT * FROM diagnostic_evidence_bundle
                WHERE tenant_id = :tenant_id AND session_id = :session_id
                  AND bundle_type = :bundle_type AND sha256 = :sha256
                """
            ),
            {
                "tenant_id": upload["tenant_id"],
                "session_id": upload["session_id"],
                "bundle_type": upload["bundle_type"],
                "sha256": sha256,
            },
        )
        return dict(result.mappings().one())

    @staticmethod
    def _assert_upload_token(row: dict[str, Any], token: str) -> None:
        supplied = hashlib.sha256(token.encode()).hexdigest()
        if not token or not secrets.compare_digest(supplied, row["upload_token_hash"]):
            raise DiagnosisError(code="INVALID_UPLOAD_TOKEN", message="直传令牌无效", http_status=403)

    @staticmethod
    def _assert_upload_active(row: dict[str, Any]) -> None:
        if row["expires_at"] <= datetime.now(UTC):
            raise DiagnosisError(code="UPLOAD_SESSION_EXPIRED", message="上传会话已过期", http_status=410)
        if row["status"] not in {"initiated", "uploading"}:
            raise DiagnosisError(
                code="UPLOAD_SESSION_NOT_ACTIVE",
                message="上传会话当前不可写",
                http_status=409,
                details={"status": row["status"]},
            )

    @staticmethod
    def _request_hash(command: UploadSessionCreate) -> str:
        canonical = json.dumps(command.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()

    @staticmethod
    def _idempotency_key(value: str) -> str:
        normalized = value.strip()
        if not normalized or len(normalized) > 128:
            raise DiagnosisError(code="INVALID_IDEMPOTENCY_KEY", message="Idempotency-Key 不合法", http_status=422)
        return normalized

    @staticmethod
    def _require_role(actor: ActorContext) -> None:
        if actor.roles.isdisjoint(UPLOAD_ROLES):
            raise DiagnosisError(code="FORBIDDEN", message="当前角色无权操作诊断证据包", http_status=403)
