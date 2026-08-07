from types import SimpleNamespace

from app.services.hci_sim_resolver import HciSimKbdResolver, KbdResolutionReport
from shared.schemas.hci_sim_policy import current_hci_sim_policy_revision
from shared.schemas.signal_generation import current_tool_contract_revision


def _entry(*, support_id: str = "27123", status: str = "published"):
    return SimpleNamespace(id=9, support_id=support_id, status=status)


def _snapshot(*, support_id: str = "27123", tool_revision: str | None = None, checksum: str = "a" * 64):
    document = {
        "schema_version": 2,
        "signals": [{"id": "sig-001", "acquire": {"tool": "qfk_system", "args": {"command": "acli system"}}}],
        "publish_validation": {
            "status": "passed",
            "tool_contract_revision": tool_revision or current_tool_contract_revision(),
        },
    }
    active = SimpleNamespace(checksum=checksum, resource_name="9")
    revision = SimpleNamespace(
        status="published",
        checksum=checksum,
        revision=24,
        trace_id="00-0123456789abcdef0123456789abcdef-0123456789abcdef-01",
        content_json={"support_id": support_id, "signals_json": document},
    )
    return active, revision


def test_resolver_freezes_active_snapshot_with_current_contracts():
    resolution = HciSimKbdResolver().resolve_entry(_entry(), _snapshot())

    assert resolution.status == "ready_for_artifact_binding"
    assert resolution.resolved is not None
    assert resolution.resolved.support_id == "27123"
    assert resolution.resolved.kbd_revision == 24
    assert resolution.resolved.kbd_checksum == "sha256:" + "a" * 64
    assert resolution.resolved.tool_contract_revision == current_tool_contract_revision()
    assert resolution.resolved.policy_revision == current_hci_sim_policy_revision()
    assert resolution.resolved.signals_digest.startswith("sha256:")


def test_resolver_fails_closed_for_missing_active_snapshot_and_unpublished_kbd():
    missing_snapshot = HciSimKbdResolver().resolve_entry(_entry(), None)
    assert missing_snapshot.status == "capability_gap"
    assert [gap.code for gap in missing_snapshot.gaps] == ["KBD_ACTIVE_SNAPSHOT_MISSING"]

    unpublished = HciSimKbdResolver().resolve_entry(_entry(status="draft"), _snapshot())
    assert unpublished.status == "capability_gap"
    assert "KBD_NOT_PUBLISHED" in [gap.code for gap in unpublished.gaps]


def test_resolver_rejects_stale_tool_contract_and_tampered_snapshot_identity():
    stale = HciSimKbdResolver().resolve_entry(_entry(), _snapshot(tool_revision="old-contract"))
    assert stale.status == "capability_gap"
    assert "TOOL_CONTRACT_STALE" in [gap.code for gap in stale.gaps]

    tampered = HciSimKbdResolver().resolve_entry(_entry(), _snapshot(support_id="other"))
    assert tampered.status == "capability_gap"
    assert "KBD_SNAPSHOT_IDENTITY_MISMATCH" in [gap.code for gap in tampered.gaps]


def test_batch_report_counts_ready_and_capability_gaps_without_claiming_artifact_readiness():
    resolver = HciSimKbdResolver()
    report = KbdResolutionReport(
        (
            resolver.resolve_entry(_entry(), _snapshot()),
            resolver.resolve_entry(_entry(support_id="27079", status="draft"), None),
        )
    ).to_dict()

    assert report["total"] == 2
    assert report["status_counts"] == {"capability_gap": 1, "ready_for_artifact_binding": 1}
    assert report["gap_counts"] == {"KBD_ACTIVE_SNAPSHOT_MISSING": 1, "KBD_NOT_PUBLISHED": 1}
    assert "未绑定获批 Artifact" in report["facts_boundary"]
