"""Diagnosis Service（诊断服务）OpenAPI 契约测试。"""

from app.main import app


def test_openapi_exposes_collector_registry_and_artifact_contracts():
    """Collector 管理和制品端点必须进入服务契约。"""

    specification = app.openapi()

    assert len(specification["paths"]) == 70
    assert "/api/diagnosis-sessions/by-case/{case_id}/workspace" in specification["paths"]
    assert "/api/diagnosis-scenarios" in specification["paths"]
    assert "/api/internal/collection-plans" in specification["paths"]
    assert "/api/internal/collection-plans/{plan_id}/regenerate" in specification["paths"]
    assert "/api/internal/collector-artifacts" in specification["paths"]
    assert "/api/internal/kbd-collection-impact/{kbd_id}" in specification["paths"]
    assert "/api/internal/collectors" in specification["paths"]
    assert "/api/internal/collectors/{collector_id}/review" in specification["paths"]
    assert "/api/diagnosis-sessions/{session_id}/collector-artifacts" in specification["paths"]
    assert "/api/diagnosis-sessions/{session_id}/collector-artifacts/{artifact_id}/download" in specification["paths"]
    assert "/api/diagnosis-sessions/{session_id}/collector-artifacts/{artifact_id}/revoke" in specification["paths"]
    assert (
        "/api/diagnosis-sessions/{session_id}/collector-artifacts/{artifact_id}/verification-bundle"
        in specification["paths"]
    )
    assert "/api/internal/collectors/security/trust-store" in specification["paths"]
    assert "/api/internal/collectors/security/revocations" in specification["paths"]
    assert "/api/diagnosis-sessions/{session_id}/uploads" in specification["paths"]
    assert "/api/direct/diagnosis-uploads/{upload_id}/parts/{part_number}" in specification["paths"]
    assert "/api/diagnosis-sessions/{session_id}/assessment" in specification["paths"]
    assert "/api/diagnosis-sessions/{session_id}/evidence/query" in specification["paths"]
    assert "/api/diagnosis-sessions/{session_id}/runs" in specification["paths"]
    assert "/api/diagnosis-sessions/{session_id}/runs/{run_id}/signals" in specification["paths"]
    assert "/api/diagnosis-sessions/{session_id}/runs/{run_id}/candidates" in specification["paths"]
    assert "/api/diagnosis-sessions/{session_id}/supplement-plan" in specification["paths"]
    assert "/api/diagnosis-sessions/{session_id}/timeline" in specification["paths"]
    assert "/api/diagnosis-sessions/{session_id}/reports" in specification["paths"]
    assert "/api/diagnosis-sessions/{session_id}/legal-hold" in specification["paths"]
    assert "/api/diagnosis-sessions/{session_id}/deletion" in specification["paths"]
    assert "/api/internal/offline-signal-mappings" in specification["paths"]
    assert "/api/internal/offline-signal-mappings/{mapping_id}" in specification["paths"]
    assert "/api/internal/offline-resource-sync/preview" in specification["paths"]
    assert "/api/internal/offline-resource-sync/history" in specification["paths"]
    assert "/api/internal/offline-resource-sync/{batch_id}/publish" in specification["paths"]
    assert "/api/internal/offline-resource-sync/{batch_id}/rollback" in specification["paths"]
    assert "/api/internal/diagnosis-security/encryption-key" in specification["paths"]
    assert "/api/internal/diagnosis-sessions" in specification["paths"]
    assert "/api/internal/diagnosis-sessions/report-reviews" in specification["paths"]
    assert "/api/internal/diagnosis-security/events" in specification["paths"]
    assert "/api/internal/diagnosis-sessions/governance" in specification["paths"]
    assert "/api/internal/diagnosis-sessions/audit" in specification["paths"]
    assert "/api/internal/collection-profiles" in specification["paths"]
    assert "/health" in specification["paths"]

    artifact_schema = specification["components"]["schemas"]["CollectorArtifactResponse"]
    assert {
        "download_path",
        "verification_bundle_path",
        "signature_base64",
        "public_key_base64",
        "public_key_fingerprint",
        "revocation_reason",
    }.issubset(artifact_schema["properties"])
    artifact_item_schema = specification["components"]["schemas"]["CollectorArtifactItemResponse"]
    assert {"rendered_command", "execution_spec"}.issubset(artifact_item_schema["properties"])
