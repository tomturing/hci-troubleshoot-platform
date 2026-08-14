from types import SimpleNamespace

from app.services.hci_sim_resolver import HciSimKbdResolver, KbdResolutionReport
from shared.schemas.hci_sim_policy import current_hci_sim_policy_revision
from shared.schemas.signal_generation import current_tool_contract_revision


def _entry(*, support_id: str = "27123", status: str = "published"):
    return SimpleNamespace(
        id=9,
        support_id=support_id,
        status=status,
        entry_metadata={"sample_suite": "diagnosis-signal-matrix-v1"},
    )


def _snapshot(*, support_id: str = "27123", tool_revision: str | None = None, checksum: str = "a" * 64):
    document = {
        "schema_version": 2,
        "signals": [
            {
                "id": "sig-001",
                "role": "must",
                "acquire": {
                    "tool": "qkv_task",
                    "args": {"keyword": "动态任务", "limit": 1, "is_failed": True},
                },
                "match": None,
                "orchestrate": {
                    "phase": "diagnostic",
                    "requires": [],
                    "produces": [{"name": "HOST", "type": "string", "path": "host"}],
                },
            }
        ],
        "verification_contract": {
            "schema_version": 1,
            "case_id": support_id,
            "scope": {
                "products": ["HCI"],
                "versions": ["*"],
                "components": ["test"],
                "topology_constraints": ["any"],
            },
            "variables": {"HOST": {"type": "string", "description": "目标主机"}},
            "evidence_policy": {
                "must": ["sig-001"],
                "should": [],
                "exclude": [],
                "context": [],
                "minimum_should": 0,
                "on_missing_must": "inconclusive",
            },
        },
        "publish_validation": {
            "schema_version": 1,
            "status": "passed",
            "tool_contract_revision": tool_revision or current_tool_contract_revision(),
            "validator": "expert_publish_gate",
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


def _tool_snapshots(*, tool: str = "qkv_task"):
    checksum = "b" * 64
    active = SimpleNamespace(checksum=checksum, resource_name=tool)
    revision = SimpleNamespace(
        status="published",
        checksum=checksum,
        revision=7,
        content_json={"tool_name": tool, "is_active": True},
    )
    return {tool: (active, revision)}


def test_resolver_freezes_active_snapshot_with_current_contracts():
    resolution = HciSimKbdResolver().resolve_entry(_entry(), _snapshot(), _tool_snapshots())

    assert resolution.status == "ready_for_artifact_binding"
    assert resolution.resolved is not None
    assert resolution.resolved.support_id == "27123"
    assert resolution.resolved.kbd_revision == 24
    assert resolution.resolved.kbd_checksum == "sha256:" + "a" * 64
    assert resolution.resolved.tool_contract_revision == current_tool_contract_revision()
    assert resolution.resolved.policy_revision == current_hci_sim_policy_revision()
    assert resolution.resolved.signals_digest.startswith("sha256:")
    assert resolution.resolved.metadata["sample_suite"] == "diagnosis-signal-matrix-v1"
    assert resolution.resolved.verification_contract["case_id"] == "27123"
    assert [route.signal_id for route in resolution.resolved.synthetic_routes] == ["sig-001"]
    assert resolution.resolved.synthetic_routes[0].argv == (
        "acli",
        "--formatter",
        "json",
        "task",
        "get",
        "-k",
        "动态任务",
        "-s",
        "failed",
        "-l",
        "1",
    )
    assert resolution.resolved.synthetic_routes[0].role == "must"
    assert resolution.resolved.synthetic_routes[0].produces[0]["name"] == "HOST"


def test_resolver_preserves_runtime_placeholders_for_lab_compiler():
    active, revision = _snapshot()
    revision.content_json["signals_json"]["signals"][0]["acquire"]["args"]["keyword"] = "{{ALERT_TYPE}}"
    resolution = HciSimKbdResolver().resolve_entry(_entry(), (active, revision), _tool_snapshots())

    assert resolution.status == "ready_for_artifact_binding"
    route = resolution.resolved.synthetic_routes[0]
    assert route.required_variables == ("ALERT_TYPE",)
    assert "{{ALERT_TYPE}}" in route.argv


def test_resolver_fails_closed_for_missing_active_snapshot_and_unpublished_kbd():
    missing_snapshot = HciSimKbdResolver().resolve_entry(_entry(), None)
    assert missing_snapshot.status == "capability_gap"
    assert [gap.code for gap in missing_snapshot.gaps] == ["KBD_ACTIVE_SNAPSHOT_MISSING"]

    unpublished = HciSimKbdResolver().resolve_entry(_entry(status="draft"), _snapshot(), _tool_snapshots())
    assert unpublished.status == "capability_gap"
    assert "KBD_NOT_PUBLISHED" in [gap.code for gap in unpublished.gaps]


def test_resolver_rejects_stale_tool_contract_and_tampered_snapshot_identity():
    stale = HciSimKbdResolver().resolve_entry(_entry(), _snapshot(tool_revision="old-contract"), _tool_snapshots())
    assert stale.status == "capability_gap"
    assert "TOOL_CONTRACT_STALE" in [gap.code for gap in stale.gaps]
    assert stale.to_dict()["metadata"]["sample_suite"] == "diagnosis-signal-matrix-v1"

    tampered = HciSimKbdResolver().resolve_entry(_entry(), _snapshot(support_id="other"), _tool_snapshots())
    assert tampered.status == "capability_gap"
    assert "KBD_SNAPSHOT_IDENTITY_MISMATCH" in [gap.code for gap in tampered.gaps]


def test_batch_report_counts_ready_and_capability_gaps_without_claiming_artifact_readiness():
    resolver = HciSimKbdResolver()
    report = KbdResolutionReport(
        (
            resolver.resolve_entry(_entry(), _snapshot(), _tool_snapshots()),
            resolver.resolve_entry(_entry(support_id="27079", status="draft"), None),
        )
    ).to_dict()

    assert report["total"] == 2
    assert report["status_counts"] == {"capability_gap": 1, "ready_for_artifact_binding": 1}
    assert report["gap_counts"] == {"KBD_ACTIVE_SNAPSHOT_MISSING": 1, "KBD_NOT_PUBLISHED": 1}
    assert "未绑定获批 Artifact" in report["facts_boundary"]


def test_resolver_does_not_allow_a_kbd_without_active_tool_revision():
    resolution = HciSimKbdResolver().resolve_entry(
        _entry(support_id="any-published-kbd"),
        _snapshot(support_id="any-published-kbd"),
        {},
    )

    assert resolution.status == "capability_gap"
    assert [gap.code for gap in resolution.gaps] == ["TOOL_ACTIVE_SNAPSHOT_MISSING"]
