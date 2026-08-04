"""Signal/Contract generation provenance and stale detection tests."""

import asyncio
from types import SimpleNamespace

import pytest
from app.routes import extract_signals
from app.routes.extract_signals import (
    ExtractSignalsResponse,
    _acquirer_catalog_prompt_text,
    _build_verification_contract,
    _matcher_quality_violation,
    _normalize_config_file_read,
    _normalize_generated_timeouts,
    _persist_signals,
    _qfk_catalog_violation,
    _qfk_command_capability_violation,
    _qfk_invocation_violation,
    _signals_to_v2,
    _unconsumed_qfk_producer_reasons,
    _validate_and_collect_signals,
)
from jsonschema import ValidationError
from shared.schemas.kbd_signal_safety import (
    signal_write_operation_command,
    validate_kbd_read_only_signals_json,
)
from shared.schemas.signal_generation import (
    build_signal_generation_metadata,
    current_tool_contract_revision,
    staleness_reasons,
)
from shared.schemas.signal_schema import validate_signals_json


def test_generation_metadata_is_deterministic_and_schema_valid():
    source = {"title": "开机失败", "images_json": [{"seq": 0, "evidence": {"x": 1}}]}
    first = build_signal_generation_metadata(
        source=source,
        prompt_template="{title}",
        model_id="gold-model",
    )
    second = build_signal_generation_metadata(
        source=source,
        prompt_template="{title}",
        model_id="gold-model",
    )
    document = _signals_to_v2([], generation_metadata=first)

    assert first == second
    assert first["tool_contract_revision"] == current_tool_contract_revision()
    validate_signals_json(document)


def test_rejected_candidates_are_persisted_for_expert_audit():
    document = _signals_to_v2(
        [],
        rejected_candidates=[
            {
                "signal": {"id": "unsafe", "acquire": {"tool": "unknown"}},
                "reason_code": "run_failed",
                "reason": "不支持的采集器",
            },
            {"signal": "not-an-object", "reason": "信号非对象"},
        ],
    )

    validate_signals_json(document)
    assert document["rejected_candidates"] == [
        {
            "candidate": {"id": "unsafe", "acquire": {"tool": "unknown"}},
            "reason_code": "run_failed",
            "reason": "不支持的采集器",
        },
        {"candidate": "not-an-object", "reason": "信号非对象"},
    ]


def test_rejected_candidate_reason_code_is_optional_for_historical_snapshots():
    document = _signals_to_v2(
        [],
        rejected_candidates=[{"signal": {"id": "legacy"}, "reason": "历史拒绝原因"}],
    )

    validate_signals_json(document)
    assert "reason_code" not in document["rejected_candidates"][0]


def test_kbd_candidate_gate_uses_three_stable_reason_codes_and_keeps_good_signal():
    candidates = [
        {
            "id": "task",
            "acquire": {
                "tool": "qkv_task",
                "args": {"keyword": "启动虚拟机", "is_failed": True},
            },
            "match": None,
            "orchestrate": {
                "phase": "diagnostic",
                "produces": [{"name": "HOST", "path": "host"}],
                "requires": [],
            },
        },
        {
            "id": "write",
            "acquire": {"tool": "qfk_system", "args": {"command": "restart"}},
            "match": {"type": "exists", "expected": True},
            "orchestrate": {"phase": "solution", "produces": [], "requires": []},
        },
        {
            "id": "missing",
            "acquire": {
                "tool": "qfk_hardware",
                "args": {"command": "mc info"},
            },
            "match": {"type": "exists", "expected": True},
            "orchestrate": {"phase": "diagnostic", "produces": [], "requires": []},
        },
        {
            "id": "invalid",
            "acquire": {
                "tool": "qfk_system",
                "args": {"command": "ps", "resource_keyword": "vm"},
            },
            "match": {"type": "exists", "expected": True},
            "orchestrate": {"phase": "diagnostic", "produces": [], "requires": []},
        },
        {
            "id": "cannot-run",
            "acquire": {"tool": "qfk_system", "args": {"command": "smartctl"}},
            "match": {"type": "exists", "expected": True},
            "orchestrate": {"phase": "diagnostic", "produces": [], "requires": []},
        },
    ]

    accepted, rejected = _validate_and_collect_signals(
        candidates,
        "kbd:test",
        enforce_kbd_read_only=True,
    )

    assert [item["id"] for item in accepted] == ["task"]
    assert [(item["signal"]["id"], item["reason_code"]) for item in rejected] == [
        ("write", "write_signal"),
        ("missing", "not_exists"),
        ("invalid", "run_failed"),
        ("cannot-run", "run_failed"),
    ]
    assert "缺少运行所需参数" in rejected[-1]["reason"]
    assert rejected[0]["signal"]["orchestrate"]["phase"] == "solution"
    assert "provenance" not in rejected[0]["signal"]


def test_post_remediation_read_only_check_is_not_misclassified_as_write_signal():
    candidate = {
        "id": "post-check",
        "acquire": {
            "tool": "qfk_system",
            "args": {"command": "lspci", "command_args": ["-vvv"]},
        },
        "match": {"type": "keyword", "pattern": "NVMe", "expected": True},
        "orchestrate": {"phase": "solution", "produces": [], "requires": []},
        "provenance": {"evidence": "重启后执行 lspci -vvv，可看到 NVMe 控制器"},
    }

    accepted, rejected = _validate_and_collect_signals(
        [candidate], "kbd:test", enforce_kbd_read_only=True
    )

    assert accepted == []
    assert rejected[0]["reason_code"] == "not_exists"
    assert rejected[0]["signal"]["orchestrate"]["phase"] == "solution"


@pytest.mark.parametrize(
    ("command", "command_args", "expected_action"),
    [
        ("soft_raid_lit", ["--off", "/dev/sda"], "--off"),
        ("sf_cli", ["disk", "light", "off", "/dev/sda"], "off"),
        (
            "strace",
            ["-f", "/sf/bin/sfscp", "/sf/data/source", "/sf/data/target"],
            "sfscp",
        ),
        ("ipmitool", ["mc", "reset", "cold"], "reset"),
    ],
)
def test_write_gate_inspects_actual_command_vector_before_catalog(
    command, command_args, expected_action
):
    candidate = {
        "id": "write-in-argv",
        "acquire": {
            "tool": "qfk_system",
            "args": {"command": command, "command_args": command_args},
        },
        "match": {"type": "exists", "expected": True},
        "orchestrate": {"phase": "diagnostic", "produces": [], "requires": []},
    }

    accepted, rejected = _validate_and_collect_signals(
        [candidate], "kbd:test", enforce_kbd_read_only=True
    )

    assert accepted == []
    assert rejected[0]["reason_code"] == "write_signal"
    assert f"写操作命令 {expected_action}" in rejected[0]["reason"]


@pytest.mark.parametrize(
    ("command", "command_args"),
    [
        ("cat", ["/var/log/power-on-history.log"]),
        ("ls", ["-l", "/sf/bin/sfscp"]),
    ],
)
def test_write_gate_does_not_treat_read_only_arguments_as_actions(command, command_args):
    candidate = {
        "id": "read-only-argument",
        "acquire": {
            "tool": "qfk_system",
            "args": {
                "command": command,
                "command_args": command_args,
            },
        },
        "match": {
            "type": "exists",
            "expected": True,
            "extract": {
                "type": "text",
                "rows": {"mode": "all"},
                "cardinality": "all",
                "source": "stdout",
            },
        },
        "orchestrate": {"phase": "diagnostic", "produces": [], "requires": []},
    }

    accepted, rejected = _validate_and_collect_signals(
        [candidate], "kbd:test", enforce_kbd_read_only=True
    )

    assert [item["id"] for item in accepted] == ["read-only-argument"]
    assert rejected == []


def test_write_gate_parses_full_read_only_command_before_scanning_argv():
    signal = {
        "acquire": {
            "tool": "qfk_system",
            "args": {"command": "/usr/bin/ls -l /sf/bin/sfscp"},
        }
    }

    assert signal_write_operation_command(signal) is None


def test_verification_contract_promotes_first_diagnostic_context_when_must_is_empty():
    signals = [
        {
            "id": "context-only",
            "role": "context",
            "acquire": {"tool": "qkv_alert", "args": {"keyword": "接收丢包率过高"}},
            "match": None,
            "orchestrate": {
                "phase": "diagnostic",
                "produces": [{"name": "HOST", "path": "host"}],
                "requires": [],
            },
        }
    ]

    contract = _build_verification_contract(
        signals,
        {"evidence_policy": {"context": ["context-only"]}},
        case_id="kbd:test",
    )

    assert signals[0]["role"] == "must"
    assert contract["evidence_policy"]["must"] == ["context-only"]
    assert contract["evidence_policy"]["context"] == []


def test_consumer_of_rejected_producer_is_run_failed_instead_of_using_ghost_variable():
    candidates = [
        {
            "id": "producer",
            "acquire": {"tool": "qfk_hardware", "args": {"command": "mc info"}},
            "match": None,
            "orchestrate": {
                "phase": "diagnostic",
                "produces": [{"name": "MC_INFO", "path": "stdout"}],
                "requires": [],
            },
        },
        {
            "id": "consumer",
            "acquire": {
                "tool": "qfk_system",
                "args": {"command": "ps", "command_args": ["{{MC_INFO}}"]},
            },
            "match": {
                "type": "exists",
                "expected": True,
                "extract": {
                    "type": "text",
                    "rows": {"mode": "all"},
                    "cardinality": "all",
                    "source": "stdout",
                },
            },
            "orchestrate": {
                "phase": "diagnostic",
                "produces": [],
                "requires": ["MC_INFO"],
            },
        },
    ]

    accepted, rejected = _validate_and_collect_signals(
        candidates,
        "kbd:test",
        enforce_kbd_read_only=True,
    )

    assert accepted == []
    assert [(item["signal"]["id"], item["reason_code"]) for item in rejected] == [
        ("producer", "not_exists"),
        ("consumer", "run_failed"),
    ]
    assert "变量依赖不可达" in rejected[1]["reason"]


def test_staleness_reports_source_prompt_model_tool_and_explicit_changes():
    current = build_signal_generation_metadata(
        source={"title": "new"},
        prompt_template="new prompt",
        model_id="new-model",
    )
    stored = dict(current)
    stored.update(
        {
            "status": "stale",
            "source_fingerprint": "0" * 64,
            "prompt_revision": "1" * 64,
            "model_id": "old-model",
            "tool_contract_revision": "2" * 64,
        }
    )

    assert staleness_reasons(stored, current) == [
        "explicitly_marked_stale",
        "source_fingerprint_changed",
        "prompt_revision_changed",
        "model_id_changed",
        "tool_contract_revision_changed",
    ]
    assert staleness_reasons(None, current) == ["generation_metadata_missing"]


def test_kbd_reextract_creates_fresh_proposal_and_clears_stale_draft_pointer(monkeypatch):
    entry = SimpleNamespace(id=27123, status="draft", signals_json={}, latest_proposal_revision_id=11, working_revision_id=13)
    calls = {}

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def flush(self):
            calls["flushed"] = True

        async def commit(self):
            calls["committed"] = True

    class FakeDbManager:
        @staticmethod
        def async_session_factory():
            return FakeSession()

    async def fake_require_mutable_kbd(_session, source_id, *, for_update=False):
        assert source_id == 27123
        assert for_update is True
        return entry

    async def fake_ensure_kbd_revision(_session, **kwargs):
        calls["revision"] = kwargs
        return SimpleNamespace(id=14)

    monkeypatch.setattr(extract_signals, "require_mutable_kbd", fake_require_mutable_kbd)
    monkeypatch.setattr(extract_signals, "ensure_kbd_revision", fake_ensure_kbd_revision)

    revision_id = asyncio.run(
        _persist_signals(
            FakeDbManager(),
            "kbd_entry",
            27123,
            [],
            generation_metadata=build_signal_generation_metadata(
                source={"title": "虚拟机开机失败"},
                prompt_template="current prompt",
                model_id="glm-5",
            ),
        )
    )

    assert revision_id == 14
    assert entry.working_revision_id is None
    assert calls["revision"]["revision_type"] == "proposal"
    assert calls["revision"]["actor_type"] == "llm"
    assert calls["revision"]["parent_revision_id"] == 11
    assert calls["revision"]["generation_metadata"]["origin"] == "signal_reextract"
    assert calls["flushed"] is True
    assert calls["committed"] is True


def test_extract_response_preserves_proposal_revision_id_for_batch_audit():
    response = ExtractSignalsResponse(
        success=True,
        kbd_id=27123,
        proposal_revision_id=14,
        signals_count=0,
    )

    assert response.model_dump()["proposal_revision_id"] == 14


def test_generated_qkv_qfk_timeouts_use_120_for_missing_and_historical_defaults():
    signals = [
        {"acquire": {"tool": "qkv_task", "args": {"keyword": "启动虚拟机"}}},
        {"acquire": {"tool": "qfk_system", "args": {"command": "cat", "timeout": 10}}},
        {"acquire": {"tool": "qfk_system", "args": {"command": "ps", "timeout": 30}}},
        {"acquire": {"tool": "qfk_system", "args": {"command": "lsof", "timeout": 180}}},
    ]

    assert _normalize_generated_timeouts(signals) == 3
    assert [signal["acquire"]["args"]["timeout"] for signal in signals] == [120, 120, 120, 180]


def test_prompt_catalog_reference_uses_current_catalog_as_knowledge_not_model_gate():
    reference = _acquirer_catalog_prompt_text()

    assert "acli hardware gpu config get" in reference
    assert "acli system ipmitool" in reference
    assert "acli hardware mc info" not in reference
    assert "缺失时仍须输出 Candidate" in reference


@pytest.mark.parametrize(
    ("tool", "args", "expected_fragment"),
    [
        ("qfk_system", {"command": "cat", "command_args": ["/sf/cfg/if.d/eth0"]}, None),
        ("qfk_system", {"command": "lspci", "command_args": []}, "不在当前 catalog"),
        ("qfk_hardware", {"command": "mc info"}, "acli hardware mc info"),
        ("qfk_hardware", {"command": "web_info"}, "acli hardware web_info"),
        ("qfk_storage", {"command": "list", "resource_keyword": "disk"}, "acli storage list"),
        ("qfk_storage", {"command": "asan disk list"}, None),
    ],
)
def test_qfk_proposal_command_must_exist_in_runtime_catalog(tool, args, expected_fragment):
    reason = _qfk_catalog_violation(tool, args)

    if expected_fragment is None:
        assert reason is None
    else:
        assert expected_fragment in reason


def test_qfk_invocation_violation_rejects_catalog_hit_that_cannot_run():
    assert "smartctl 至少需要 1 个命令参数" in _qfk_invocation_violation(
        "qfk_system", {"command": "smartctl"}
    )
    assert (
        _qfk_invocation_violation(
            "qfk_system", {"command": "smartctl", "command_args": ["--scan"]}
        )
        is None
    )


def test_ipmitool_mc_info_cannot_claim_raid_firmware_capability():
    signal = {
        "acquire": {
            "tool": "qfk_system",
            "args": {
                "command": "ipmitool",
                "command_args": ["mc", "info"],
                "instruction": "检查 RAID 卡固件版本",
            },
        },
        "provenance": {"evidence": "适配器固件版本为 51.13"},
    }

    assert "只能采集 BMC/MC 信息" in _qfk_command_capability_violation(signal)


def test_regex_matcher_must_match_its_own_evidence():
    reason = _matcher_quality_violation(
        {"type": "regex", "pattern": "^(9H|F6H)"},
        evidence="SN为9H、F6H开头服务器",
    )

    assert "无法命中 provenance.evidence" in reason
    assert (
        _matcher_quality_violation(
            {"type": "regex", "pattern": "core-lcore-slave-0-\\d+-\\d+"},
            evidence="core-lcore-slave-0-55166-1710466980",
        )
        is None
    )


@pytest.mark.parametrize(
    ("candidate", "expected"),
    [
        (
            {
                "acquire": {
                    "tool": "qkv_alert",
                    "args": {"keyword": "The host was restarted by BMC."},
                },
                "match": None,
                "orchestrate": {
                    "produces": [{"name": "HOST", "path": "host"}],
                    "requires": [],
                },
                "provenance": {"evidence": "The host was restarted by BMC."},
            },
            "外部事件日志不是 HCI 平台告警",
        ),
        (
            {
                "acquire": {"tool": "qfk_log", "args": {"file": "messages"}},
                "match": {"type": "keyword", "pattern": "51.13", "expected": True},
                "orchestrate": {"produces": [], "requires": []},
                "provenance": {"evidence": "截图可见文字：51.13.0-3427"},
            },
            "采集来源无法验证",
        ),
        (
            {
                "acquire": {"tool": "qfk_log", "args": {"file": "vmid.conf"}},
                "match": {"type": "keyword", "pattern": "vtool_installed", "expected": True},
                "orchestrate": {"produces": [], "requires": []},
                "provenance": {
                    "evidence": "cat `find /cfs/nodes/ -name vmid.conf` | grep vtool_installed"
                },
            },
            "不能把配置文件 vmid.conf 作为日志",
        ),
    ],
)
def test_acquisition_must_be_supported_by_candidate_evidence(candidate, expected):
    accepted, rejected = _validate_and_collect_signals(
        [candidate], "kbd:test", enforce_kbd_read_only=True
    )

    assert accepted == []
    assert rejected[0]["reason_code"] == "run_failed"
    assert expected in rejected[0]["reason"]


def test_log_shaped_evidence_keeps_normal_qfk_log_candidate():
    candidate = {
        "acquire": {"tool": "qfk_log", "args": {"file": "messages"}},
        "match": {
            "type": "keyword",
            "pattern": "MCE: Killing",
            "expected": True,
            "extract": {
                "type": "text",
                "rows": {"mode": "all"},
                "cardinality": "all",
                "source": "stdout",
            },
        },
        "orchestrate": {"produces": [], "requires": []},
        "provenance": {"evidence": "err [kernel:] MCE: Killing due to memory fault"},
    }

    accepted, rejected = _validate_and_collect_signals(
        [candidate], "kbd:test", enforce_kbd_read_only=True
    )

    assert len(accepted) == 1
    assert rejected == []


@pytest.mark.parametrize(
    ("matcher", "expected_fragment"),
    [
        ({"type": "keyword", "pattern": "address xx.100.88"}, "脱敏占位文本"),
        ({"type": "regex", "pattern": r"host-***"}, "脱敏占位文本"),
        ({"type": "keyword", "pattern": "530-8i|530-16i"}, "不解释正则竖线"),
        ({"type": "exists", "pattern": "HDD"}, "不读取 match.pattern"),
        ({"type": "keyword", "pattern": ["530-8i", "530-16i"]}, None),
        ({"type": "regex", "pattern": r"530-(8i|16i)"}, None),
        ({"type": "exists"}, None),
    ],
)
def test_matcher_quality_gate_rejects_silent_false_positive_and_impossible_patterns(matcher, expected_fragment):
    reason = _matcher_quality_violation(matcher)

    if expected_fragment is None:
        assert reason is None
    else:
        assert expected_fragment in reason


def test_kbd30880_exists_candidate_null_pattern_is_normalized_before_schema_gate():
    candidate = {
        "id": "sig_002",
        "role": "must",
        "acquire": {
            "tool": "qfk_system",
            "args": {
                "host": "{{HOST}}",
                "command": "cat",
                "command_args": ["/sf/cfg/gpu_info.ini"],
                "timeout": 120,
                "instruction": "查看报错主机的GPU配置文件",
            },
        },
        "match": {
            "type": "exists",
            "pattern": None,
            "expected": True,
            "extract": {
                "type": "text",
                "rows": {"mode": "all"},
                "cardinality": "all",
                "source": "stdout",
            },
        },
        "orchestrate": {"phase": "diagnostic", "produces": [], "requires": ["HOST"]},
        "provenance": {"evidence": "查看报错主机的/sf/cfg/gpu_info.ini配置文件"},
    }

    accepted, rejected = _validate_and_collect_signals(
        [candidate], "kbd:30880", enforce_kbd_read_only=True
    )

    assert len(accepted) == 1
    assert rejected == []
    assert accepted[0]["match"]["type"] == "exists"
    assert "pattern" not in accepted[0]["match"]


def test_keyword_matcher_must_be_traceable_in_candidate_evidence():
    matcher = {"type": "keyword", "pattern": "address"}

    assert "无法从 provenance.evidence 逐字追溯" in _matcher_quality_violation(
        matcher,
        evidence="管理口为channel4，但是eth0口也残留了跟管理口一样的ip",
    )


def test_state_matcher_must_use_a_literal_state_from_candidate_evidence():
    assert "state Matcher" in _matcher_quality_violation(
        {"type": "state", "pattern": "aggregationNum"},
        evidence="正常情况下聚合内各成员口的对端聚合 ID 值相同",
    )
    assert (
        _matcher_quality_violation(
            {"type": "state", "pattern": "running"},
            evidence="服务状态应为 running",
        )
        is None
    )
    assert "530-16i" in _matcher_quality_violation(
        {"type": "keyword", "pattern": ["530-8i", "530-16i"]},
        evidence="Subsystem: Lenovo ThinkSystem RAID 530-8i PCIe 12Gb Adapter",
    )
    assert (
        _matcher_quality_violation(
            {"type": "keyword", "pattern": ["530-8i", "530-16i"]},
            evidence="支持 530-8i 或 530-16i",
        )
        is None
    )


def test_unconsumed_qfk_producer_is_rejected_but_real_downstream_consumer_is_allowed():
    dead = {
        "id": "sig_001",
        "acquire": {"tool": "qfk_system", "args": {"command": "cat"}},
        "match": None,
        "orchestrate": {"produces": [{"name": "GPU_INFO_CONTENT", "path": "stdout"}], "requires": []},
    }
    self_reference = {
        "id": "sig_002",
        "acquire": {"tool": "qfk_system", "args": {"command": "cat"}},
        "match": None,
        "orchestrate": {"produces": [{"name": "SELF", "path": "stdout"}], "requires": ["SELF"]},
    }

    reasons = _unconsumed_qfk_producer_reasons([dead, self_reference])
    assert "GPU_INFO_CONTENT" in reasons[id(dead)]
    assert "SELF" in reasons[id(self_reference)]

    consumer = {
        "id": "sig_003",
        "acquire": {"tool": "qfk_system", "args": {"command": "ps"}},
        "match": {"type": "keyword", "pattern": "qemu"},
        "orchestrate": {"produces": [], "requires": ["GPU_INFO_CONTENT"]},
    }
    assert id(dead) not in _unconsumed_qfk_producer_reasons([dead, consumer])


def test_dead_qfk_producer_gate_records_rejected_candidate_reason():
    dead = {
        "id": "sig_001",
        "role": "must",
        "acquire": {
            "tool": "qfk_system",
            "args": {"command": "cat", "command_args": ["/sf/cfg/gpu_info.ini"], "timeout": 10},
        },
        "match": None,
        "orchestrate": {
            "phase": "diagnostic",
            "produces": [{"name": "GPU_INFO_CONTENT", "path": "stdout"}],
            "requires": [],
        },
        "provenance": {"category": "backend", "source_section": "steps_text", "evidence": "读取配置"},
        "review": {"require_human_confirm": False},
    }

    accepted, rejected = _validate_and_collect_signals([dead], source_id="KBD30880")

    assert accepted == []
    # Rejected Candidate 保存模型原始对象；工作副本虽归一为 120，但不得冒充原始输出。
    assert rejected[0]["signal"]["acquire"]["args"]["timeout"] == 10
    assert "未被任何下游信号消费" in rejected[0]["reason"]


def test_kbd_solution_candidate_is_rejected_before_match_or_produces_gate():
    candidate = {
        "id": "sig_003",
        "role": "context",
        "acquire": {
            "tool": "qfk_system",
            "args": {
                "command": "sed",
                "command_args": ["-i", "/gpu_type/d", "/sf/cfg/gpu_info.ini"],
                "timeout": 120,
            },
        },
        "match": None,
        "orchestrate": {"phase": "solution", "produces": [], "requires": ["HOST"]},
        "provenance": {
            "category": "backend",
            "source_section": "steps_text",
            "evidence": "取消gpu_type字段",
        },
        "review": {"require_human_confirm": True},
    }

    accepted, rejected = _validate_and_collect_signals(
        [candidate],
        source_id="KBD30880",
        enforce_kbd_read_only=True,
    )

    assert accepted == []
    assert rejected[0]["signal"]["orchestrate"]["requires"] == ["HOST"]
    assert candidate["orchestrate"]["requires"] == []
    assert "处置动作不属于 KBD 关键信号" in rejected[0]["reason"]
    assert "match 或 orchestrate.produces" not in rejected[0]["reason"]


def test_kbd_read_only_match_signal_remains_accepted():
    candidate = {
        "id": "sig_001",
        "role": "must",
        "acquire": {
            "tool": "qfk_system",
            "args": {
                "command": "cat",
                "command_args": ["/sf/cfg/gpu_info.ini"],
                "timeout": 120,
            },
        },
        "match": {
            "type": "keyword",
            "pattern": "gpu_type",
            "mode": "or",
            "expected": True,
            "extract": {
                "type": "text",
                "rows": {"mode": "all"},
                "cardinality": "all",
                "source": "stdout",
            },
        },
        "orchestrate": {"phase": "diagnostic", "produces": [], "requires": ["HOST"]},
        "provenance": {
            "category": "backend",
            "source_section": "steps_text",
            "evidence": "检查gpu_type字段",
            "confidence": 0.9,
        },
        "review": {"require_human_confirm": False},
    }

    accepted, rejected = _validate_and_collect_signals(
        [candidate],
        source_id="KBD-read-only",
        enforce_kbd_read_only=True,
    )

    assert rejected == []
    assert [signal["id"] for signal in accepted] == ["sig_001"]


def test_kbd_expert_save_gate_rejects_explicit_write_action():
    document = {
        "schema_version": 2,
        "signals": [
            {
                "id": "sig_001",
                "acquire": {"tool": "qfk_service", "args": {"command": "restart"}},
                "orchestrate": {"phase": "diagnostic", "produces": [], "requires": []},
            }
        ],
    }

    with pytest.raises(ValidationError, match="检测到写操作命令 restart"):
        validate_kbd_read_only_signals_json(document)


def test_sop_extraction_keeps_existing_solution_annotation_behavior():
    candidate = {
        "id": "sig_001",
        "role": "context",
        "acquire": {
            "tool": "qfk_service",
            "args": {"resource_keyword": "sfvt-apache", "command": "restart"},
        },
        "match": {
            "type": "state",
            "pattern": "running",
            "mode": "or",
            "expected": True,
            "extract": {
                "type": "text",
                "rows": {"mode": "all"},
                "cardinality": "all",
                "source": "stdout",
            },
        },
        "orchestrate": {"phase": "diagnostic", "produces": [], "requires": []},
        "provenance": {"category": "backend", "source_section": "steps_text"},
        "review": {"require_human_confirm": False},
    }

    accepted, rejected = _validate_and_collect_signals([candidate], source_id="sop:1")

    assert rejected == []
    assert accepted[0]["orchestrate"]["phase"] == "solution"
    assert accepted[0]["review"]["require_human_confirm"] is True


def test_config_file_normalization_uses_current_qfk_system_command_args_contract():
    signal = {
        "acquire": {
            "tool": "qfk_log",
            "args": {"path": "/sf/cfg", "file": "gpu_info.ini", "timeout": 30},
        },
        "review": {},
    }

    assert _normalize_config_file_read(signal) is True
    assert signal["acquire"] == {
        "tool": "qfk_system",
        "args": {
            "command": "cat",
            "command_args": ["/sf/cfg/gpu_info.ini"],
            "timeout": 30,
        },
    }
    assert "resource_keyword" not in signal["acquire"]["args"]
