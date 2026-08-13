"""Collector 端到端 PostgreSQL 冒烟测试。"""

import base64
import hashlib
import io
import json
import os
import tarfile
from datetime import UTC, datetime, timedelta
from uuid import uuid4
from zipfile import ZipFile

import pytest
from app.auth import ActorContext, InternalCaseAuthorizer
from app.config import settings
from app.domain.signed_document import canonical_json_bytes
from app.errors import DiagnosisError
from app.repositories.collection_plan_repository import CollectionPlanRepository
from app.repositories.collector_artifact_repository import CollectorArtifactRepository
from app.repositories.diagnosis_session_repository import DiagnosisSessionRepository
from app.schemas.collection_plan import CollectionPlanGenerateRequest, CollectionPlanRegenerateRequest
from app.schemas.collection_profile import CollectionProfilePublishRequest
from app.schemas.collector_artifact import CollectorArtifactGenerateRequest
from app.schemas.collector_definition import CollectorApprovalRequest, CollectorDefinitionWrite
from app.schemas.diagnosis_session import DiagnosisSessionCreate
from app.schemas.evidence_lifecycle import LegalHoldRequest, OfflineSignalMappingWrite, UploadSessionCreate
from app.services.artifact_signer import Ed25519ArtifactSigner
from app.services.bundle_processor import BundleProcessor
from app.services.collection_plan_service import CollectionPlanService
from app.services.collection_profile_service import CollectionProfileService
from app.services.collector_artifact_service import CollectorArtifactService
from app.services.collector_definition_service import CollectorDefinitionService
from app.services.collector_trust_service import CollectorTrustService
from app.services.diagnosis_management_service import DiagnosisManagementService
from app.services.diagnosis_session_service import DiagnosisSessionService
from app.services.envelope_encryption import EnvelopeEncryptionService
from app.services.evidence_upload_service import EvidenceUploadService
from app.services.object_storage import LocalObjectStorage
from app.services.offline_analysis_service import OfflineAnalysisService
from app.services.offline_governance_service import OfflineGovernanceService
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from shared.dynamic_resource.publisher import DynamicResourcePublisher
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("RUN_DIAGNOSIS_POSTGRES_INTEGRATION") != "1",
        reason="需要显式启用本地 PostgreSQL 集成测试",
    ),
]


@pytest.mark.asyncio
async def test_collector_pipeline_uses_approved_revision_and_produces_verifiable_artifact(tmp_path, monkeypatch):
    """事务内验证画像、计划、审批修订和签名制品全链路。"""

    monkeypatch.setattr(settings, "DIAGNOSIS_ENABLE_AUTOMATIC_SUPPLEMENT", True)
    database_url = os.environ["TEST_DATABASE_URL"]
    engine = create_async_engine(database_url)
    connection = await engine.connect()
    transaction = await connection.begin()
    session = AsyncSession(bind=connection, expire_on_commit=False)
    try:
        case_id = (
            await session.execute(
                text(
                    """
                    SELECT case_id
                    FROM "case"
                    WHERE case_id ~ '^Q[0-9]{12,13}$'
                    ORDER BY created_at DESC NULLS LAST
                    LIMIT 1
                    """
                )
            )
        ).scalar_one()
        actor = ActorContext(
            tenant_id=f"collector-smoke-{uuid4().hex[:8]}",
            user_id="diagnosis-integration-test",
            roles=frozenset({"platform_admin", "diagnosis_worker"}),
        )
        collector_id = f"collector.smoke.{uuid4().hex[:12]}"
        supplement_collector_id = f"collector.smoke.supplement.{uuid4().hex[:12]}"
        profile_id = "vm_backup_failed"
        kbd_signals = {
            "schema_version": "2.0",
            "signals": [
                {
                    "id": "collector-smoke-task",
                    "acquire": {"tool": "qkv_task", "args": {"keyword": "备份", "is_failed": True}},
                }
            ],
        }
        kbd_support_id = f"T{uuid4().hex[:12]}"
        kbd_id = (
            await session.execute(
                text(
                    """
                    INSERT INTO kbd_entry (
                        support_id, title, problem_description, root_cause, solution,
                        signals_json, metadata, category_id, status
                    ) VALUES (
                        :support_id, 'Collector 冒烟 KBD', '备份失败', '测试根因', '测试方案',
                        CAST(:signals AS jsonb), '{}'::jsonb, NULL, 'published'
                    ) RETURNING id
                    """
                ),
                {
                    "support_id": kbd_support_id,
                    "signals": json.dumps(kbd_signals),
                },
            )
        ).scalar_one()
        kbd_snapshot = await DynamicResourcePublisher(session).ensure_published(
            resource_type="kbd",
            resource_name=str(kbd_id),
            version="1.0",
            content={
                "id": kbd_id,
                "support_id": kbd_support_id,
                "title": "Collector 冒烟 KBD",
                "category_id": None,
                "signals_json": kbd_signals,
                "status": "published",
            },
            contract={"agent_usable": True, "metadata": {"offline_scenario": profile_id}},
            trace_id="collector-smoke-kbd",
        )

        definition_service = CollectorDefinitionService(session)
        draft = await definition_service.save_draft(
            actor=actor,
            collector_id=collector_id,
            command=CollectorDefinitionWrite.model_validate(
                {
                    "collector_id": collector_id,
                    "display_name": "主机名采集",
                    "description": "只读获取主机名",
                    "command_template": "/bin/hostname",
                    "parameter_schema": {
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                    "timeout_seconds": 10,
                    "max_output_mb": 1,
                    "supported_product_versions": ["6.*"],
                    "output_contract": {
                        "schema_id": "hostname_text_v1",
                        "media_type": "text/plain",
                        "output_path": "commands/hostname.txt",
                    },
                    "version": "1.0.0",
                }
            ),
            if_match=None,
        )
        approved = await definition_service.approve(
            actor=actor,
            collector_id=collector_id,
            command=CollectorApprovalRequest(approved=True),
            if_match=str(draft.lock_version),
        )
        listed_collectors = await definition_service.list(
            actor=actor,
            review_status="approved",
            is_enabled=True,
        )
        assert any(item.collector_id == collector_id for item in listed_collectors)
        assert approved.active_revision is not None

        supplement_draft = await definition_service.save_draft(
            actor=actor,
            collector_id=supplement_collector_id,
            command=CollectorDefinitionWrite.model_validate(
                {
                    "collector_id": supplement_collector_id,
                    "display_name": "补充采集",
                    "description": "只读补充采集主机信息",
                    "command_template": "/bin/hostname -f",
                    "parameter_schema": {
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                    "timeout_seconds": 10,
                    "max_output_mb": 1,
                    "supported_product_versions": ["6.*"],
                    "output_contract": {
                        "schema_id": "hostname_fqdn_text_v1",
                        "media_type": "text/plain",
                        "output_path": "commands/hostname-fqdn.txt",
                    },
                    "version": "1.0.0",
                }
            ),
            if_match=None,
        )
        await definition_service.approve(
            actor=actor,
            collector_id=supplement_collector_id,
            command=CollectorApprovalRequest(approved=True),
            if_match=str(supplement_draft.lock_version),
        )

        # 画像审批门禁要求所有引用 Collector 已批准并启用。
        profile_service = CollectionProfileService(session)
        await profile_service.publish(
            actor=actor,
            profile_id=profile_id,
            command=CollectionProfilePublishRequest.model_validate(
                {
                    "version": "smoke-1.0.0",
                    "profile": {
                        "profile_id": profile_id,
                        "display_name": "Collector 冒烟采集画像",
                        "product_line": "HCI",
                        "scenario": profile_id,
                        "supported_product_versions": ["6.*"],
                        "items": [
                            {
                                "collector_id": collector_id,
                                "display_name": "主机名采集",
                                "required_level": "mandatory",
                                "target_scope": "once",
                                "reason": "验证签名制品链路",
                                "expected_size_mb": 1,
                                "timeout_seconds": 10,
                            },
                            {
                                "collector_id": supplement_collector_id,
                                "display_name": "补充采集",
                                "required_level": "deep_dive",
                                "target_scope": "once",
                                "reason": "首次结论不足时补充证据",
                                "expected_size_mb": 1,
                                "timeout_seconds": 10,
                            },
                        ],
                    },
                }
            ),
        )

        repository = DiagnosisSessionRepository(session)
        session_result = await DiagnosisSessionService(
            repository,
            case_authorizer=InternalCaseAuthorizer(session),
            scenario_availability=profile_service,
        ).create(
            actor=actor,
            command=DiagnosisSessionCreate.model_validate(
                {
                    "case_id": case_id,
                    "product_line": "HCI",
                    "selected_scenario": profile_id,
                    "selected_category": "虚拟机备份与CDP",
                    "incident": {
                        "start_time": datetime.now(UTC) - timedelta(minutes=30),
                        "end_time": datetime.now(UTC),
                        "timezone": "Asia/Shanghai",
                    },
                    "affected_objects": [{"type": "vm", "id": "vm-smoke"}],
                    "impact_scope": "single_vm",
                    "current_status": "ongoing",
                }
            ),
            idempotency_key=f"session-{uuid4()}",
        )
        plan_service = CollectionPlanService(
            session=session,
            session_repository=repository,
            plan_repository=CollectionPlanRepository(session),
        )
        plan_result = await plan_service.generate(
            actor=actor,
            session_id=str(session_result.entity.session_id),
            command=CollectionPlanGenerateRequest(product_version="6.10.0"),
            idempotency_key=f"plan-{uuid4()}",
        )
        assert plan_result.entity.plan_revision == 1
        assert plan_result.entity.kbd_ruleset_snapshot
        assert all(item["revision"] and item["checksum"] for item in plan_result.entity.kbd_ruleset_snapshot)
        assert len(plan_result.entity.kbd_ruleset_checksum) == 64
        assert all(item.collector_revision and item.collector_checksum for item in plan_result.items)
        assert next(item for item in plan_result.items if item.collector_id == collector_id).collector_revision == (
            approved.active_revision
        )
        original_plan_id = plan_result.entity.plan_id
        regeneration_key = f"plan-regenerate-{uuid4()}"
        plan_result = await plan_service.regenerate(
            actor=actor,
            plan_id=str(original_plan_id),
            command=CollectionPlanRegenerateRequest(reason="集成测试验证计划修订"),
            idempotency_key=regeneration_key,
        )
        assert plan_result.entity.plan_revision == 2
        assert all(item.collector_revision and item.collector_checksum for item in plan_result.items)
        replayed_plan = await plan_service.regenerate(
            actor=actor,
            plan_id=str(original_plan_id),
            command=CollectionPlanRegenerateRequest(reason="集成测试验证计划修订"),
            idempotency_key=regeneration_key,
        )
        assert replayed_plan.created is False
        assert replayed_plan.entity.plan_id == plan_result.entity.plan_id

        private_key = Ed25519PrivateKey.generate()
        signer = Ed25519ArtifactSigner(
            private_key_base64=base64.b64encode(private_key.private_bytes_raw()).decode("ascii"),
            key_id="collector-smoke-key",
        )
        encryption_private_key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
        encryption_private_pem = encryption_private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        encryption = EnvelopeEncryptionService(
            private_key_base64=base64.b64encode(encryption_private_pem).decode(),
            key_id="evidence-smoke-key",
        )
        artifact_repository = CollectorArtifactRepository(session)
        artifact_result = await CollectorArtifactService(
            session=session,
            session_repository=repository,
            plan_repository=CollectionPlanRepository(session),
            artifact_repository=artifact_repository,
            signer=signer,
            envelope_encryption=encryption,
        ).generate(
            actor=actor,
            session_id=str(session_result.entity.session_id),
            command=CollectorArtifactGenerateRequest(collection_plan_id=plan_result.entity.plan_id),
            idempotency_key=f"artifact-{uuid4()}",
        )

        artifact = artifact_result.entity
        assert artifact_result.created is True
        assert len(artifact_result.items) == 1
        assert artifact_result.items[0].collector_revision == approved.active_revision
        artifact_document = json.loads(artifact.content_text)
        assert artifact_document["schema_version"] == "1.0"
        assert artifact_document["execution_items"][0]["argv"] == ["/bin/hostname"]
        assert artifact_document["execution_items"][0]["executor"] == "command"
        assert artifact_document["execution_items"][0]["max_output_bytes"] > 0
        assert "/bin/sh" not in artifact.content_text
        assert artifact.manifest_json["kbd_ruleset"]["checksum"] == plan_result.entity.kbd_ruleset_checksum
        Ed25519PublicKey.from_public_bytes(base64.b64decode(artifact.public_key_base64)).verify(
            base64.b64decode(artifact.signature_base64),
            artifact.content_text.encode("utf-8"),
        )
        manifest = dict(artifact.manifest_json)
        manifest_signature = manifest.pop("document_signature")
        private_key.public_key().verify(
            base64.b64decode(manifest_signature["signature_base64"]),
            canonical_json_bytes(manifest),
        )
        assert manifest["schema_version"] == "1.2"
        assert manifest["artifact_type"] == "structured_collector"

        bundle = await CollectorTrustService(
            artifact_repository=artifact_repository,
            session_repository=repository,
            signer=signer,
        ).build_verification_bundle(
            actor=actor,
            session_id=str(session_result.entity.session_id),
            artifact_id=str(artifact.artifact_id),
        )
        with ZipFile(io.BytesIO(bundle.content)) as archive:
            assert {
                artifact.file_name,
                "artifact-manifest.json",
                "runtime-manifest.json",
                "trust-store.json",
                "revocations.json",
                "hci-collect-linux-amd64",
                "case.json",
                "README.txt",
            }.issubset(archive.namelist())
            embedded_case = json.loads(archive.read("case.json"))
        assert embedded_case["case_id"] == case_id
        assert embedded_case["selected_scenario"] == profile_id
        assert embedded_case["incident_timezone"] == "Asia/Shanghai"
        assert embedded_case["targets"] == [{"type": "vm", "id": "vm-smoke"}]
        assert set(embedded_case["incident_window"]) == {"start", "end"}

        evidence_content = b"hostname-node-smoke\n"
        evidence_path = "commands/hostname.stdout"
        evidence_manifest = {
            "schema_version": "1.0",
            "bundle_id": str(uuid4()),
            "case_id": case_id,
            "session_id": str(session_result.entity.session_id),
            "bundle_type": "initial",
            "parent_bundle_id": None,
            "selected_scenario": profile_id,
            "collection_profile_version": plan_result.entity.profile_version,
            "collection_plan_id": str(plan_result.entity.plan_id),
            "collector_artifact_version": artifact.schema_version,
            "collector_artifact_sha256": artifact.artifact_sha256,
            "signature_key_id": artifact.signing_key_id,
            "generated_at": datetime.now(UTC).isoformat(),
            "incident_window": {
                "start": session_result.entity.incident_start_time.isoformat(),
                "end": session_result.entity.incident_end_time.isoformat(),
            },
            "targets": [{"type": "vm", "id": "vm-smoke"}],
            "collection_items": [
                {
                    "collector_id": collector_id,
                    "status": "success",
                    "source": "all",
                    "source_timezone": "Asia/Shanghai",
                    "clock_offset_ms": 0,
                    "time_coverage": {
                        "start": session_result.entity.incident_start_time.isoformat(),
                        "end": session_result.entity.incident_end_time.isoformat(),
                    },
                    "files": [
                        {
                            "path": evidence_path,
                            "original_name": "hostname.stdout",
                            "media_type": "text/plain",
                            "sensitivity": "internal",
                            "size_bytes": len(evidence_content),
                            "sha256": hashlib.sha256(evidence_content).hexdigest(),
                        }
                    ],
                    "exit_code": 0,
                    "failure_reason": None,
                }
            ],
            "encryption": {
                "algorithm": "AES-256-GCM",
                "key_id": "evidence-smoke-key",
                "encrypted_data_key": "envelope-header",
            },
        }
        plain_bundle = tmp_path / "evidence.tar.gz"
        with tarfile.open(plain_bundle, "w:gz") as archive:
            for name, content in {
                "case.json": json.dumps({"case_id": case_id}).encode(),
                "manifest.json": json.dumps(evidence_manifest).encode(),
                evidence_path: evidence_content,
            }.items():
                info = tarfile.TarInfo(name)
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))

        encrypted_bundle = tmp_path / "evidence.hci-eb"
        EnvelopeEncryptionService.encrypt_file(
            source=plain_bundle,
            target=encrypted_bundle,
            public_key_pem=encryption_private_key.public_key().public_bytes(
                serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            ),
            key_id=encryption.key_id,
        )
        storage = LocalObjectStorage(str(tmp_path / "object-storage"))
        upload_service = EvidenceUploadService(session=session, storage=storage)
        upload_result = await upload_service.create(
            actor=actor,
            session_id=str(session_result.entity.session_id),
            command=UploadSessionCreate.model_validate(
                {
                    "bundle_type": "initial",
                    "collection_plan_id": str(plan_result.entity.plan_id),
                    "collector_artifact_id": str(artifact.artifact_id),
                    "file_name": encrypted_bundle.name,
                    "media_type": "application/vnd.hci.evidence",
                    "total_size_bytes": encrypted_bundle.stat().st_size,
                    "sha256": hashlib.sha256(encrypted_bundle.read_bytes()).hexdigest(),
                    "chunk_size_bytes": 1024 * 1024,
                }
            ),
            idempotency_key=f"upload-{uuid4()}",
            source_ip="127.0.0.1",
        )

        async def encrypted_chunks():
            with encrypted_bundle.open("rb") as source:
                while chunk := source.read(64 * 1024):
                    yield chunk

        await upload_service.record_part(
            upload_id=str(upload_result.row["upload_id"]),
            part_number=1,
            token=upload_result.token,
            chunks=encrypted_chunks(),
            claimed_sha256=None,
        )
        uploaded_bundle, created = await upload_service.complete(
            actor=actor,
            session_id=str(session_result.entity.session_id),
            upload_id=str(upload_result.row["upload_id"]),
            part_numbers=[1],
        )
        assert created is True
        task_id = (
            await session.execute(
                text("SELECT task_id FROM diagnosis_processing_job WHERE bundle_id = :bundle_id"),
                {"bundle_id": uploaded_bundle["bundle_id"]},
            )
        ).scalar_one()
        await BundleProcessor(
            session=session,
            storage=storage,
            encryption=encryption,
            worker_id="integration-test",
        ).process(task_id=str(task_id))

        processed = (
            (
                await session.execute(
                    text(
                        "SELECT processing_status, failure_code FROM diagnostic_evidence_bundle WHERE bundle_id = :bundle_id"
                    ),
                    {"bundle_id": uploaded_bundle["bundle_id"]},
                )
            )
            .mappings()
            .one()
        )
        assert processed["processing_status"] == "ready", processed
        assert (
            await session.execute(
                text("SELECT COUNT(*) FROM evidence_item WHERE bundle_id = :bundle_id"),
                {"bundle_id": uploaded_bundle["bundle_id"]},
            )
        ).scalar_one() == 1
        report = (
            (
                await session.execute(
                    text("SELECT diagnosis_level, publish_status FROM diagnosis_report WHERE session_id = :session_id"),
                    {"session_id": session_result.entity.session_id},
                )
            )
            .mappings()
            .one()
        )
        assert report["diagnosis_level"] in {"Confirmed", "Probable", "Suspected", "Insufficient", "Conflicted"}
        assert report["publish_status"] == "draft"

        analysis = OfflineAnalysisService(session)
        runs = await analysis.list_runs(actor=actor, session_id=str(session_result.entity.session_id))
        assert len(runs) == 1
        signal_rows = await analysis.list_signal_evaluations(
            actor=actor,
            session_id=str(session_result.entity.session_id),
            run_id=str(runs[0]["run_id"]),
        )
        candidate_rows = await analysis.list_diagnosis_candidates(
            actor=actor,
            session_id=str(session_result.entity.session_id),
            run_id=str(runs[0]["run_id"]),
        )
        assert isinstance(signal_rows, list)
        assert isinstance(candidate_rows, list)

        governance = OfflineGovernanceService(session)
        supplement_plan = await governance.get_supplement_plan(
            actor=actor,
            session_id=str(session_result.entity.session_id),
        )
        assert supplement_plan["parent_bundle_id"] == uploaded_bundle["bundle_id"]
        assert supplement_plan["collection_plan_id"] != plan_result.entity.plan_id
        assert supplement_plan["status"] == "ready"

        supplement_artifact_result = await CollectorArtifactService(
            session=session,
            session_repository=repository,
            plan_repository=CollectionPlanRepository(session),
            artifact_repository=artifact_repository,
            signer=signer,
            envelope_encryption=encryption,
        ).generate(
            actor=actor,
            session_id=str(session_result.entity.session_id),
            command=CollectorArtifactGenerateRequest(
                collection_plan_id=supplement_plan["collection_plan_id"],
            ),
            idempotency_key=f"supplement-artifact-{uuid4()}",
        )
        supplement_artifact = supplement_artifact_result.entity
        assert [item.collector_id for item in supplement_artifact_result.items] == [supplement_collector_id]

        supplement_content = b"hostname-node-smoke.example.test\n"
        supplement_path = "commands/hostname-fqdn.stdout"
        supplement_manifest = {
            "schema_version": "1.0",
            "bundle_id": str(uuid4()),
            "case_id": case_id,
            "session_id": str(session_result.entity.session_id),
            "bundle_type": "supplement",
            "parent_bundle_id": str(uploaded_bundle["bundle_id"]),
            "selected_scenario": profile_id,
            "collection_profile_version": plan_result.entity.profile_version,
            "collection_plan_id": str(supplement_plan["collection_plan_id"]),
            "collector_artifact_version": supplement_artifact.schema_version,
            "collector_artifact_sha256": supplement_artifact.artifact_sha256,
            "signature_key_id": supplement_artifact.signing_key_id,
            "generated_at": datetime.now(UTC).isoformat(),
            "incident_window": {
                "start": session_result.entity.incident_start_time.isoformat(),
                "end": session_result.entity.incident_end_time.isoformat(),
            },
            "targets": [{"type": "vm", "id": "vm-smoke"}],
            "collection_items": [
                {
                    "collector_id": supplement_collector_id,
                    "status": "success",
                    "source": "all",
                    "source_timezone": "Asia/Shanghai",
                    "clock_offset_ms": 0,
                    "time_coverage": {
                        "start": session_result.entity.incident_start_time.isoformat(),
                        "end": session_result.entity.incident_end_time.isoformat(),
                    },
                    "files": [
                        {
                            "path": supplement_path,
                            "original_name": "hostname-fqdn.stdout",
                            "media_type": "text/plain",
                            "sensitivity": "internal",
                            "size_bytes": len(supplement_content),
                            "sha256": hashlib.sha256(supplement_content).hexdigest(),
                        }
                    ],
                    "exit_code": 0,
                    "failure_reason": None,
                }
            ],
            "encryption": {
                "algorithm": "AES-256-GCM",
                "key_id": "evidence-smoke-key",
                "encrypted_data_key": "envelope-header",
            },
        }
        supplement_plain_bundle = tmp_path / "supplement-evidence.tar.gz"
        with tarfile.open(supplement_plain_bundle, "w:gz") as archive:
            for name, content in {
                "case.json": json.dumps({"case_id": case_id}).encode(),
                "manifest.json": json.dumps(supplement_manifest).encode(),
                supplement_path: supplement_content,
            }.items():
                info = tarfile.TarInfo(name)
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
        encrypted_supplement_bundle = tmp_path / "supplement-evidence.hci-eb"
        EnvelopeEncryptionService.encrypt_file(
            source=supplement_plain_bundle,
            target=encrypted_supplement_bundle,
            public_key_pem=encryption_private_key.public_key().public_bytes(
                serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            ),
            key_id=encryption.key_id,
        )
        supplement_upload_result = await upload_service.create(
            actor=actor,
            session_id=str(session_result.entity.session_id),
            command=UploadSessionCreate.model_validate(
                {
                    "bundle_type": "supplement",
                    "parent_bundle_id": str(uploaded_bundle["bundle_id"]),
                    "collection_plan_id": str(supplement_plan["collection_plan_id"]),
                    "collector_artifact_id": str(supplement_artifact.artifact_id),
                    "file_name": encrypted_supplement_bundle.name,
                    "media_type": "application/vnd.hci.evidence",
                    "total_size_bytes": encrypted_supplement_bundle.stat().st_size,
                    "sha256": hashlib.sha256(encrypted_supplement_bundle.read_bytes()).hexdigest(),
                    "chunk_size_bytes": 1024 * 1024,
                }
            ),
            idempotency_key=f"supplement-upload-{uuid4()}",
            source_ip="127.0.0.1",
        )

        async def supplement_chunks():
            with encrypted_supplement_bundle.open("rb") as source:
                while chunk := source.read(64 * 1024):
                    yield chunk

        await upload_service.record_part(
            upload_id=str(supplement_upload_result.row["upload_id"]),
            part_number=1,
            token=supplement_upload_result.token,
            chunks=supplement_chunks(),
            claimed_sha256=None,
        )
        supplement_bundle, supplement_created = await upload_service.complete(
            actor=actor,
            session_id=str(session_result.entity.session_id),
            upload_id=str(supplement_upload_result.row["upload_id"]),
            part_numbers=[1],
        )
        assert supplement_created is True
        supplement_task_id = (
            await session.execute(
                text("SELECT task_id FROM diagnosis_processing_job WHERE bundle_id = :bundle_id"),
                {"bundle_id": supplement_bundle["bundle_id"]},
            )
        ).scalar_one()
        await BundleProcessor(
            session=session,
            storage=storage,
            encryption=encryption,
            worker_id="integration-test",
        ).process(task_id=str(supplement_task_id))

        runs_after_supplement = await analysis.list_runs(
            actor=actor,
            session_id=str(session_result.entity.session_id),
        )
        assert [row["run_sequence"] for row in runs_after_supplement] == [1, 2]
        report_states = (
            (
                await session.execute(
                    text(
                        """
                    SELECT report_sequence, publish_status
                    FROM diagnosis_report
                    WHERE session_id = :session_id
                    ORDER BY report_sequence
                    """
                    ),
                    {"session_id": session_result.entity.session_id},
                )
            )
            .mappings()
            .all()
        )
        assert [(row["report_sequence"], row["publish_status"]) for row in report_states] == [
            (1, "superseded"),
            (2, "draft"),
        ]
        completed_supplement = await governance.get_supplement_plan(
            actor=actor,
            session_id=str(session_result.entity.session_id),
        )
        assert completed_supplement["status"] == "completed"

        management = DiagnosisManagementService(session)
        managed = await management.list_sessions(
            actor=actor,
            query=str(session_result.entity.session_id),
            status=None,
            assigned_to=None,
            offset=0,
            limit=10,
        )
        assert managed["total"] == 1
        assigned = await management.assign(
            actor=actor,
            session_id=str(session_result.entity.session_id),
            assigned_to="integration-owner",
        )
        assert assigned["details"]["assigned_to"] == "integration-owner"
        management_audit = await management.list_audit(actor=actor, limit=100)
        assert any(
            item["record_type"] == "management_access" and item["status"] == "assign" for item in management_audit
        )

        await governance.save_signal_mapping(
            actor=actor,
            mapping_id=str(uuid4()),
            command=OfflineSignalMappingWrite(
                source_kbd_id=kbd_id,
                source_kbd_revision=kbd_snapshot.revision,
                source_signal_id="collector-smoke-task",
                execution_contract_checksum=hashlib.sha256(b"collector-smoke-task-contract").hexdigest(),
                acquire_tool="qkv_task",
                category_scope="*",
                command_scope="备份",
                collector_id=collector_id,
                query_type="json",
                priority=50,
            ),
            if_match=None,
        )
        assert await governance.list_signal_mappings(actor=actor)
        published_kbd_id = (
            await session.execute(
                text(
                    """
                    SELECT id FROM kbd_entry
                    WHERE status = 'published'
                      AND jsonb_array_length(COALESCE(signals_json->'signals', '[]'::jsonb)) > 0
                    ORDER BY id LIMIT 1
                    """
                )
            )
        ).scalar_one()
        impact = await governance.analyze_kbd_collection_impact(actor=actor, kbd_id=published_kbd_id)
        assert impact["requirements"]
        assert "affected_profiles" in impact
        timeline = await governance.get_timeline(
            actor=actor,
            session_id=str(session_result.entity.session_id),
        )
        assert {"session_created", "bundle", "assessment", "diagnosis_run", "report"}.issubset(
            {item["event_type"] for item in timeline}
        )
        hold = await governance.update_legal_hold(
            actor=actor,
            session_id=str(session_result.entity.session_id),
            command=LegalHoldRequest(action="apply", reason="集成测试法务保全"),
        )
        assert hold["legal_hold"] is True
        with pytest.raises(DiagnosisError) as exc_info:
            await governance.update_legal_hold(
                actor=actor,
                session_id=str(session_result.entity.session_id),
                command=LegalHoldRequest(action="release", reason="同人解除应被拒绝"),
            )
        assert getattr(exc_info.value, "code", None) == "LEGAL_HOLD_FOUR_EYES_REQUIRED"
        release_actor = ActorContext(
            tenant_id=actor.tenant_id,
            user_id="diagnosis-integration-approver",
            roles=frozenset({"platform_admin"}),
        )
        released = await governance.update_legal_hold(
            actor=release_actor,
            session_id=str(session_result.entity.session_id),
            command=LegalHoldRequest(action="release", reason="第二管理员审批解除"),
        )
        assert released["legal_hold"] is False
        disabled = await definition_service.disable(
            actor=actor,
            collector_id=collector_id,
            if_match=str(approved.lock_version),
        )
        assert disabled.is_enabled is False
        artifact_status = (
            await session.execute(
                text("SELECT status FROM collector_artifact WHERE artifact_id = :artifact_id"),
                {"artifact_id": artifact.artifact_id},
            )
        ).scalar_one()
        assert artifact_status == "revoked"
    finally:
        await transaction.rollback()
        await session.close()
        await connection.close()
        await engine.dispose()
