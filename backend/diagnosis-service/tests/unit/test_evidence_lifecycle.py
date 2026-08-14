"""离线证据包、安全处理和结论策略测试。"""

import base64
import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest
from app.config import settings
from app.domain.evidence_bundle import SafeBundleExtractor, bounded_structured_data
from app.errors import DiagnosisError
from app.routes.evidence_lifecycle import _upload_response
from app.schemas.evidence_lifecycle import BundleType, UploadSessionCreate
from app.services.envelope_encryption import EnvelopeEncryptionService
from app.services.object_storage import LocalObjectStorage
from app.services.offline_analysis_service import (
    OfflineAnalysisService,
    _conclusion,
    _evaluate_matcher,
    _missing_evidence_metadata,
)
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


def test_upload_target_defaults_to_ui_same_origin(monkeypatch):
    """本地上传地址必须由浏览器解析到当前 UI Origin（来源）。"""

    monkeypatch.setattr(settings, "DIAGNOSIS_DIRECT_UPLOAD_BASE_URL", "/")
    row = {
        "upload_id": "11111111-1111-1111-1111-111111111111",
        "session_id": "22222222-2222-2222-2222-222222222222",
        "status": "created",
        "bundle_type": "initial",
        "total_size_bytes": 1024 * 1024,
        "chunk_size_bytes": 1024 * 1024,
        "part_count": 1,
        "uploaded_parts": {},
        "expires_at": "2026-08-04T00:00:00+08:00",
        "trace_id": "a" * 32,
    }

    response = _upload_response(row, "t" * 32)

    assert response.upload_targets[0].upload_url == (
        "/api/direct/diagnosis-uploads/11111111-1111-1111-1111-111111111111/parts/1"
    )


def test_assessment_prefers_plan_item_link_over_reported_hostname():
    """实际主机名与节点 IP 不同时，已绑定计划项的证据仍必须计入完整度。"""

    plan_item_id = "11111111-1111-1111-1111-111111111111"
    assessment = OfflineAnalysisService._calculate_assessment(
        [
            {
                "item_id": plan_item_id,
                "collector_id": "kbd_qfk_network",
                "display_name": "KBD 网络状态采集",
                "required_level": "mandatory",
                "activation_state": "active",
                "target": {"type": "node", "id": "10.97.128.10"},
            }
        ],
        [
            {
                "evidence_id": "22222222-2222-2222-2222-222222222222",
                "collection_plan_item_id": plan_item_id,
                "collector_id": "kbd_qfk_network",
                "source_object": {"id": "host-00e0eccad1ec", "source_node": "host-00e0eccad1ec"},
                "evidence_status": "available",
            }
        ],
        ["33333333-3333-3333-3333-333333333333"],
        {"profile_name": "vm_backup_failed"},
    )

    assert assessment["algorithm_version"] == "completeness-v3"
    assert assessment["completeness_score"] == 100
    assert assessment["mandatory_available"] == 1
    assert assessment["ready_for_diagnosis"] is True


def test_missing_evidence_exposes_bounded_collection_failure_reason():
    """缺失证据应呈现退出原因和有界 stderr，不泄露或返回无界采集输出。"""

    metadata = _missing_evidence_metadata(
        [
            {
                "evidence_status": "collection_failed",
                "failure_reason": "collector_exit_127",
                "source_path": "commands/system.stderr",
                "structured_data": {
                    "preview": "journalctl: command not found\nHCI_API_TOKEN=should-not-be-returned\n" + "x" * 800
                },
            }
        ]
    )

    assert metadata["status"] == "collection_failed"
    assert metadata["failure_reasons"] == ["collector_exit_127"]
    assert metadata["failure_details"][0].startswith("journalctl: command not found")
    assert "should-not-be-returned" not in metadata["failure_details"][0]
    assert "[REDACTED]" in metadata["failure_details"][0]
    assert len(metadata["failure_details"][0]) == 500


def test_missing_evidence_marks_historical_link_mismatch():
    """历史评估标记为缺失但证据实际可用时，应明确标识旧算法关联异常。"""

    metadata = _missing_evidence_metadata(
        [
            {
                "evidence_status": "available",
                "failure_reason": None,
                "source_path": "commands/network.stdout",
                "structured_data": {},
            }
        ]
    )

    assert metadata["status"] == "assessment_link_mismatch"
    assert metadata["failure_reasons"] == ["historical_assessment_link_mismatch"]


def test_missing_evidence_classifies_product_version_and_strips_ansi():
    metadata = _missing_evidence_metadata(
        [
            {
                "evidence_status": "collection_failed",
                "failure_reason": "collector_exit_1",
                "source_path": "commands/vm.stderr",
                "structured_data": {"preview": "\x1b[31m错误：当前命令仅支持6.12.0及以上版本\x1b[0m"},
            }
        ]
    )

    assert metadata["failure_reasons"] == ["collector_product_version_unsupported", "collector_exit_1"]
    assert metadata["failure_details"] == ["错误：当前命令仅支持6.12.0及以上版本"]


def _manifest(content: bytes) -> dict:
    return {
        "schema_version": "1.0",
        "bundle_id": "eb-test",
        "case_id": "Q2026072900001",
        "session_id": "session-test",
        "bundle_type": "initial",
        "parent_bundle_id": None,
        "selected_scenario": "vm_start_failed",
        "collection_profile_version": "1.0.0",
        "collection_plan_id": "plan-test",
        "collector_artifact_version": "1.1",
        "collector_artifact_sha256": "a" * 64,
        "signature_key_id": "collector-key",
        "generated_at": "2026-07-29T10:00:00+08:00",
        "incident_window": {
            "start": "2026-07-29T09:30:00+08:00",
            "end": "2026-07-29T10:00:00+08:00",
        },
        "targets": [{"type": "vm", "id": "vm-1", "source_node": "node-1"}],
        "collection_items": [
            {
                "collector_id": "hci_failed_tasks",
                "status": "success",
                "source": "node-1",
                "source_timezone": "Asia/Shanghai",
                "clock_offset_ms": 0,
                "time_coverage": {
                    "start": "2026-07-29T09:30:00+08:00",
                    "end": "2026-07-29T10:00:00+08:00",
                },
                "files": [
                    {
                        "path": "commands/tasks.stdout",
                        "original_name": "tasks.stdout",
                        "media_type": "text/plain",
                        "sensitivity": "internal",
                        "size_bytes": len(content),
                        "sha256": hashlib.sha256(content).hexdigest(),
                    }
                ],
                "exit_code": 0,
                "failure_reason": None,
            }
        ],
        "encryption": {
            "algorithm": "AES-256-GCM",
            "key_id": "evidence-key",
            "encrypted_data_key": "stored-in-envelope-header",
        },
    }


def _bundle(path: Path, content: bytes = b"task failed: image busy\n") -> None:
    documents = {
        "case.json": json.dumps({"case_id": "Q2026072900001"}).encode(),
        "manifest.json": json.dumps(_manifest(content)).encode(),
        "commands/tasks.stdout": content,
    }
    with tarfile.open(path, "w:gz") as archive:
        for name, value in documents.items():
            info = tarfile.TarInfo(name)
            info.size = len(value)
            archive.addfile(info, io.BytesIO(value))


def test_safe_bundle_extractor_validates_declared_hashes(tmp_path):
    archive = tmp_path / "bundle.tar.gz"
    _bundle(archive)
    extracted = SafeBundleExtractor(
        max_files=10,
        max_file_bytes=1024 * 1024,
        max_extracted_bytes=4 * 1024 * 1024,
    ).extract(archive, tmp_path / "work")

    assert extracted.manifest.collection_items[0].collector_id == "hci_failed_tasks"
    assert extracted.security_results["hashes_valid"] is True


def test_text_matcher_index_is_not_truncated_at_64_kib(tmp_path):
    """命令输出在共享 4 MiB 契约内必须完整进入确定性 Matcher。"""

    path = tmp_path / "large.stdout"
    path.write_text("x" * (70 * 1024) + "\ncritical-marker\n", encoding="utf-8")

    structured = bounded_structured_data(path, "text/plain")

    assert structured["truncated"] is False
    assert "critical-marker" in structured["preview"]


def test_safe_bundle_extractor_rejects_path_traversal(tmp_path):
    archive = tmp_path / "unsafe.tar.gz"
    with tarfile.open(archive, "w:gz") as output:
        info = tarfile.TarInfo("../escape")
        info.size = 1
        output.addfile(info, io.BytesIO(b"x"))

    with pytest.raises(DiagnosisError) as exc_info:
        SafeBundleExtractor(max_files=10, max_file_bytes=1024, max_extracted_bytes=4096).extract(
            archive, tmp_path / "work"
        )

    assert exc_info.value.code == "UNSAFE_BUNDLE_PATH"
    assert not (tmp_path / "escape").exists()


def test_envelope_encryption_round_trip_and_tamper_rejection(tmp_path):
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    private_pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    service = EnvelopeEncryptionService(
        private_key_base64=base64.b64encode(private_pem).decode(),
        key_id="evidence-key-2026",
    )
    public_pem = base64.b64decode(service.public_metadata()["public_key_pem_base64"])
    source = tmp_path / "plain.tar.gz"
    source.write_bytes(b"\x1f\x8btrusted evidence")
    encrypted = tmp_path / "bundle.hci-eb"
    EnvelopeEncryptionService.encrypt_file(
        source=source,
        target=encrypted,
        public_key_pem=public_pem,
        key_id=service.key_id,
    )
    decrypted = tmp_path / "decrypted.tar.gz"

    metadata = service.decrypt_file(encrypted, decrypted)

    assert decrypted.read_bytes() == source.read_bytes()
    assert metadata["algorithm"] == "AES-256-GCM"
    tampered = bytearray(encrypted.read_bytes())
    tampered[-1] ^= 1
    encrypted.write_bytes(tampered)
    with pytest.raises(DiagnosisError) as exc_info:
        service.decrypt_file(encrypted, tmp_path / "rejected.tar.gz")
    assert exc_info.value.code == "BUNDLE_DECRYPTION_FAILED"


@pytest.mark.asyncio
async def test_local_object_storage_streams_parts_and_whole_hash(tmp_path):
    storage = LocalObjectStorage(str(tmp_path / "objects"))

    async def chunks():
        yield b"abc"
        yield b"def"

    size, digest = await storage.write_part(
        upload_id="11111111-1111-1111-1111-111111111111",
        part_number=1,
        chunks=chunks(),
        max_bytes=6,
    )
    whole_size, whole_digest = await storage.complete_multipart(
        upload_id="11111111-1111-1111-1111-111111111111",
        part_numbers=[1],
        object_key="quarantine/tenant/session/bundle.hci-eb",
        max_bytes=6,
    )

    assert size == whole_size == 6
    assert digest == whole_digest == hashlib.sha256(b"abcdef").hexdigest()


def test_bundle_type_parent_contract_is_fail_closed():
    common = {
        "collection_plan_id": "11111111-1111-1111-1111-111111111111",
        "collector_artifact_id": "22222222-2222-2222-2222-222222222222",
        "file_name": "bundle.hci-eb",
        "media_type": "application/vnd.hci.evidence",
        "total_size_bytes": 100,
        "sha256": "a" * 64,
    }
    with pytest.raises(ValueError):
        UploadSessionCreate(bundle_type=BundleType.SUPPLEMENT, **common)


def test_unknown_never_becomes_counter_evidence_in_conclusion():
    assessment = {
        "ready_for_diagnosis": True,
        "completeness_score": 80,
        "mandatory_available": 1,
        "mandatory_total": 2,
    }
    candidates = [
        {
            "score": 0.7,
            "matched_count": 2,
            "not_matched_count": 0,
            "unknown_count": 3,
        }
    ]

    assert _conclusion(assessment, candidates)["level"] == "Probable"
    assert _evaluate_matcher({"type": "keyword", "pattern": "busy"}, {"preview": "image busy"}) is True
    assert _evaluate_matcher({"type": "numeric", "path": "latency", "operator": "gt", "value": 100}, {}) is None


def test_p0_automatic_supplement_is_disabled_by_default():
    """精简 P0 不得在未显式配置时把客户会话推进到自动补采。"""

    assert settings.DIAGNOSIS_ENABLE_AUTOMATIC_SUPPLEMENT is False


def test_signal_evaluation_only_consumes_exact_kbd_revision_mapping():
    """同工具同分类的其他 Signal 证据不得串入当前 Signal。"""

    service = OfflineAnalysisService(None)
    mappings = [
        {
            "source_kbd_id": 101,
            "source_kbd_revision": 3,
            "source_signal_id": "signal-a",
            "execution_contract_checksum": "a" * 64,
            "acquire_tool": "qfk_log",
            "category_scope": "storage",
            "command_scope": "*",
            "collector_id": "collector-a",
            "priority": 50,
        },
        {
            "source_kbd_id": 101,
            "source_kbd_revision": 3,
            "source_signal_id": "signal-b",
            "execution_contract_checksum": "b" * 64,
            "acquire_tool": "qfk_log",
            "category_scope": "storage",
            "command_scope": "*",
            "collector_id": "collector-b",
            "priority": 50,
        },
    ]
    evidence = [
        {
            "evidence_id": "evidence-a",
            "collector_id": "collector-a",
            "evidence_status": "available",
            "structured_data": {"preview": "disk busy"},
        },
        {
            "evidence_id": "evidence-b",
            "collector_id": "collector-b",
            "evidence_status": "available",
            "structured_data": {"preview": "unrelated output"},
        },
    ]

    result = service._evaluate_signal(
        101,
        3,
        "KBD-101",
        "storage",
        {
            "id": "signal-a",
            "acquire": {"tool": "qfk_log", "args": {}},
            "match": {
                "type": "keyword",
                "pattern": "busy",
                "expected": True,
                "extract": {"type": "text", "rows": {"mode": "all"}, "cardinality": "all"},
            },
        },
        evidence,
        mappings,
    )

    assert result["state"] == "MATCHED"
    assert result["evidence_refs"] == ["evidence-a"]


def test_kbd_explicit_collector_cannot_bypass_exact_mapping():
    """KBD 历史 collector_id 不能把未映射采集器升级为可信证据来源。"""

    service = OfflineAnalysisService(None)
    result = service._evaluate_signal(
        101,
        3,
        "KBD-101",
        "storage",
        {
            "id": "signal-a",
            "acquire": {
                "tool": "qfk_log",
                "args": {},
                "offline": {"collector_id": "collector-unapproved"},
            },
            "match": {"type": "keyword", "pattern": "busy", "expected": True},
        },
        [
            {
                "evidence_id": "evidence-unapproved",
                "collector_id": "collector-unapproved",
                "evidence_status": "available",
                "structured_data": {"preview": "disk busy"},
            }
        ],
        [
            {
                "source_kbd_id": 101,
                "source_kbd_revision": 3,
                "source_signal_id": "signal-a",
                "execution_contract_checksum": "a" * 64,
                "acquire_tool": "qfk_log",
                "category_scope": "storage",
                "command_scope": "*",
                "collector_id": "collector-approved",
                "priority": 50,
            }
        ],
    )

    assert result["state"] == "UNKNOWN"
    assert result["evidence_refs"] == []


def test_signal_without_acquisition_contract_cannot_consume_arbitrary_evidence():
    """缺少采集契约的 Signal 必须失败关闭，不能扫描本次运行的全部证据。"""

    result = OfflineAnalysisService(None)._evaluate_signal(
        101,
        3,
        "KBD-101",
        "storage",
        {"id": "signal-without-acquire", "match": {"type": "keyword", "pattern": "busy"}},
        [
            {
                "evidence_id": "unrelated-evidence",
                "collector_id": "unrelated-collector",
                "evidence_status": "available",
                "structured_data": {"preview": "disk busy"},
            }
        ],
        [],
    )

    assert result["state"] == "UNKNOWN"
    assert result["evidence_refs"] == []


@pytest.mark.asyncio
async def test_run_manifest_uses_frozen_mapping_snapshot_without_live_query():
    """运行清单必须只由 Plan 快照生成，不能回读当前映射表。"""

    class RejectLiveQuerySession:
        async def execute(self, *_args, **_kwargs):
            raise AssertionError("Run Manifest 不得查询实时数据库")

    service = OfflineAnalysisService(RejectLiveQuerySession())
    mapping = {
        "mapping_id": "mapping-1",
        "source_kbd_id": 101,
        "source_kbd_revision": 3,
        "source_signal_id": "signal-a",
        "collector_id": "collector-a",
    }
    manifest = await service._run_manifest(
        session_row={"session_id": "session-1", "version": 1, "selected_scenario": "storage"},
        plan={
            "profile_name": "storage",
            "profile_revision": 2,
            "profile_version": "1.0.0",
            "profile_checksum": "p" * 64,
            "plan_id": "plan-1",
            "plan_sequence": 1,
            "plan_revision": 1,
            "request_hash": "r" * 64,
            "kbd_ruleset_checksum": "k" * 64,
            "kbd_ruleset_snapshot": [{"offline_signal_mappings": [mapping]}],
        },
        bundles=[{"bundle_id": "bundle-1", "sha256": "b" * 64, "schema_version": "1.0", "bundle_type": "initial"}],
        assessment_id="assessment-1",
    )

    assert manifest["offline_signal_mapping"]["count"] == 1
    assert manifest["offline_signal_mapping"]["entries"] == [mapping]
