from types import SimpleNamespace

from app.services.hci_sim_resolver import HciSimKbdResolver, KbdResolutionReport, _derive_sample_output
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


def test_resolver_derives_consumable_stdout_from_kbd_evidence():
    active, revision = _snapshot()
    revision.content_json["signals_json"]["signals"][0]["provenance"] = {
        "evidence": "QemuMonitor: Completed 10 of 10 bytes",
    }
    resolution = HciSimKbdResolver().resolve_entry(_entry(), (active, revision), _tool_snapshots())

    assert resolution.status == "ready_for_artifact_binding"
    route = resolution.resolved.synthetic_routes[0]
    assert '"evidence":"QemuMonitor: Completed 10 of 10 bytes"' in route.sample_output
    assert route.sample_source == "kbd_provenance_evidence"


def test_resolver_freezes_qkv_output_processing_contract_metadata():
    active, revision = _snapshot()
    signal = revision.content_json["signals_json"]["signals"][0]
    signal["orchestrate"]["produces"] = [{"name": "DESCRIPTION", "type": "string", "path": "description"}]
    signal["orchestrate"]["output_processing"] = [{
        "mode": "derive",
        "input": "{{DESCRIPTION}}",
        "name": "VM_NAME",
        "type": "string",
        "extract": {"type": "feature", "feature": "vm_name", "cardinality": "exactly_one"},
    }]
    resolution = HciSimKbdResolver().resolve_entry(_entry(), (active, revision), _tool_snapshots())

    assert resolution.status == "ready_for_artifact_binding"
    route = resolution.resolved.synthetic_routes[0]
    assert route.output_processing[0]["name"] == "VM_NAME"
    assert route.derived_variables == ("VM_NAME",)
    assert route.processing_fingerprint.startswith("sha256:")
    assert route.to_dict()["output_processing"]


def test_resolver_fails_closed_for_missing_active_snapshot_and_unpublished_kbd():
    missing_snapshot = HciSimKbdResolver().resolve_entry(_entry(), None)
    assert missing_snapshot.status == "capability_gap"
    assert [gap.code for gap in missing_snapshot.gaps] == ["KBD_ACTIVE_SNAPSHOT_MISSING"]

    unpublished = HciSimKbdResolver().resolve_entry(_entry(status="draft"), _snapshot(), _tool_snapshots())
    assert unpublished.status == "capability_gap"
    assert "KBD_NOT_PUBLISHED" in [gap.code for gap in unpublished.gaps]


def test_resolver_allows_stale_tool_contract_per_validator_driven_gate_and_rejects_tampered_identity():
    """对齐 #755 校验器驱动门禁：tool_contract_revision 字节哈希仅作变化探测，不据此阻断。
    旧信号能否在新契约下执行由 playbooks 路由的顶层 validate_publishable_signals_json 判定。
    """
    # 用有效的 64 位 hex 值（与当前 hash 不同）模拟 stale 契约
    stale_hash = "0" * 64
    stale = HciSimKbdResolver().resolve_entry(_entry(), _snapshot(tool_revision=stale_hash), _tool_snapshots())
    assert stale.status == "ready_for_artifact_binding"
    assert not stale.gaps  # 无阻断，tool_revision 仅追溯用
    assert stale.resolved.tool_contract_revision == stale_hash

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


def test_resolver_rejects_partial_route_set_when_any_signal_is_unresolved():
    active, revision = _snapshot()
    revision.content_json["signals_json"]["signals"].append(
        {
            "id": "sig-blocked",
            "role": "must",
            "acquire": {"tool": "qkv_task", "args": {}},
            "orchestrate": {},
        }
    )

    resolution = HciSimKbdResolver().resolve_entry(_entry(), (active, revision), _tool_snapshots())

    assert resolution.status == "capability_gap"
    assert any(gap.code == "SYNTHETIC_ROUTE_UNRESOLVED" for gap in resolution.gaps)
    assert resolution.resolved is None


def test_custom_producer_sample_output_uses_shared_variable_template():
    signal = {
        "acquire": {"args": {}},
        "match": {},
        "orchestrate": {
            "produces": [{"name": "CUSTOM_DISK", "type": "string", "path": "disk"}],
        },
    }

    output = _derive_sample_output(signal, "qkv_task", "producer", "acli task get")

    assert '"disk":"{{CUSTOM_DISK}}"' in output


def test_resolver_resolves_working_draft_revision():
    """验证工作稿（KbdRevision）态下的信号可被正确解析为 Bundle 编译输入。"""
    document = {
        "schema_version": 2,
        "signals": [
            {
                "id": "expert_1788139182831_f2fcdaefb29d",
                "role": "must",
                "acquire": {
                    "tool": "qkv_task",
                    "args": {"keyword": "删除虚拟机", "limit": 1, "is_failed": True},
                },
                "match": None,
                "orchestrate": {
                    "phase": "diagnostic",
                    "requires": [],
                    "produces": [{"name": "VM", "type": "string", "path": "vm"}],
                },
            }
        ],
        "verification_contract": {
            "schema_version": 1,
            "case_id": "41446",
            "evidence_policy": {"must": ["expert_1788139182831_f2fcdaefb29d"]},
        },
        "publish_validation": {
            "schema_version": 1,
            "status": "passed",
            "tool_contract_revision": current_tool_contract_revision(),
            "validator": "expert_publish_gate",
        },
    }
    kbd_rev = SimpleNamespace(
        revision_no=23,
        checksum="c" * 64,
        payload_json={"signals_json": document, "metadata": {"sample_suite": "working-draft-v1"}},
        trace_id="test-trace-rev-23",
    )
    resolution = HciSimKbdResolver().resolve_revision(_entry(support_id="41446"), kbd_rev, _tool_snapshots())

    assert resolution.status == "ready_for_artifact_binding"
    assert resolution.resolved is not None
    assert resolution.resolved.support_id == "41446"
    assert resolution.resolved.kbd_revision == 23
    assert resolution.resolved.kbd_checksum == "sha256:" + "c" * 64
    assert resolution.resolved.source_trace_id == "test-trace-rev-23"
    assert [route.signal_id for route in resolution.resolved.synthetic_routes] == ["expert_1788139182831_f2fcdaefb29d"]


from unittest.mock import AsyncMock, MagicMock
import pytest


@pytest.mark.asyncio
async def test_resolve_support_id_mode_1_published_default():
    """【模式一：先发布再测试】未传 revision 时默认从 active 发布快照解析。"""
    resolver = HciSimKbdResolver()
    entry = _entry(support_id="27123", status="published")
    active, revision = _snapshot(support_id="27123")
    
    session = AsyncMock()
    # 模拟查询 KbdEntry
    entry_result = MagicMock()
    entry_result.scalar_one_or_none.return_value = entry
    session.execute.return_value = entry_result

    # 模拟 _active_snapshots 与 _active_tool_snapshots
    resolver._active_snapshots = AsyncMock(return_value={"9": (active, revision)})
    resolver._active_tool_snapshots = AsyncMock(return_value=_tool_snapshots())

    resolution = await resolver.resolve_support_id(session, "27123")

    assert resolution.status == "ready_for_artifact_binding"
    assert resolution.resolved is not None
    assert resolution.resolved.support_id == "27123"
    assert resolution.resolved.kbd_revision == 24
    assert [r.signal_id for r in resolution.resolved.synthetic_routes] == ["sig-001"]


@pytest.mark.asyncio
async def test_resolve_support_id_mode_1_unpublished_fails():
    """【模式一：先发布再测试】未发布 KBD 在不指定 revision 时严格阻断（保持原有安全基线）。"""
    resolver = HciSimKbdResolver()
    entry = _entry(support_id="27123", status="draft")  # 尚未发布
    active, revision = _snapshot(support_id="27123")

    session = AsyncMock()
    entry_result = MagicMock()
    entry_result.scalar_one_or_none.return_value = entry
    session.execute.return_value = entry_result

    resolver._active_snapshots = AsyncMock(return_value={"9": (active, revision)})
    resolver._active_tool_snapshots = AsyncMock(return_value=_tool_snapshots())

    resolution = await resolver.resolve_support_id(session, "27123")

    assert resolution.status == "capability_gap"
    assert any(g.code == "KBD_NOT_PUBLISHED" for g in resolution.gaps)
    assert resolution.resolved is None


@pytest.mark.asyncio
async def test_resolve_support_id_mode_2_working_draft():
    """【模式二：先测试再发布】传入 revision 时从工作稿 KbdRevision 解析，不要求 KbdEntry 处于 published 状态。"""
    resolver = HciSimKbdResolver()
    entry = _entry(support_id="41446", status="draft")  # 工作稿态，未发布
    
    document = {
        "schema_version": 2,
        "signals": [
            {
                "id": "expert_1788139182831_f2fcdaefb29d",
                "role": "must",
                "acquire": {
                    "tool": "qkv_task",
                    "args": {"keyword": "删除虚拟机", "limit": 1, "is_failed": True},
                },
                "match": None,
                "orchestrate": {
                    "phase": "diagnostic",
                    "requires": [],
                    "produces": [{"name": "VM", "type": "string", "path": "vm"}],
                },
            }
        ],
        "verification_contract": {
            "schema_version": 1,
            "case_id": "41446",
            "evidence_policy": {"must": ["expert_1788139182831_f2fcdaefb29d"]},
        },
        "publish_validation": {
            "schema_version": 1,
            "status": "passed",
            "tool_contract_revision": current_tool_contract_revision(),
            "validator": "expert_publish_gate",
        },
    }
    kbd_rev = SimpleNamespace(
        revision_no=23,
        checksum="c" * 64,
        payload_json={"signals_json": document, "metadata": {"sample_suite": "working-draft-v1"}},
        trace_id="trace-rev-23",
    )

    session = AsyncMock()
    # 第一次 execute 查 KbdEntry，第二次 execute 查 KbdRevision
    entry_result = MagicMock()
    entry_result.scalar_one_or_none.return_value = entry
    rev_result = MagicMock()
    rev_result.scalar_one_or_none.return_value = kbd_rev
    session.execute.side_effect = [entry_result, rev_result]

    resolver._active_tool_snapshots = AsyncMock(return_value=_tool_snapshots())

    resolution = await resolver.resolve_support_id(session, "41446", revision=23)

    assert resolution.status == "ready_for_artifact_binding"
    assert resolution.resolved is not None
    assert resolution.resolved.support_id == "41446"
    assert resolution.resolved.kbd_revision == 23
    assert [r.signal_id for r in resolution.resolved.synthetic_routes] == ["expert_1788139182831_f2fcdaefb29d"]


