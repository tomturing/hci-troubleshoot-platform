"""Signal/Contract generation provenance and stale detection tests."""

import asyncio
from types import SimpleNamespace

from app.routes import extract_signals
from app.routes.extract_signals import (
    _normalize_config_file_read,
    _normalize_generated_timeouts,
    _persist_signals,
    _signals_to_v2,
    _unconsumed_qfk_producer_reasons,
    _validate_and_collect_signals,
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
                "reason": "不支持的采集器",
            },
            {"signal": "not-an-object", "reason": "信号非对象"},
        ],
    )

    validate_signals_json(document)
    assert document["rejected_candidates"] == [
        {
            "candidate": {"id": "unsafe", "acquire": {"tool": "unknown"}},
            "reason": "不支持的采集器",
        },
        {"candidate": "not-an-object", "reason": "信号非对象"},
    ]


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


def test_generated_qkv_qfk_timeouts_use_120_for_missing_and_historical_defaults():
    signals = [
        {"acquire": {"tool": "qkv_task", "args": {"keyword": "启动虚拟机"}}},
        {"acquire": {"tool": "qfk_system", "args": {"command": "cat", "timeout": 10}}},
        {"acquire": {"tool": "qfk_system", "args": {"command": "ps", "timeout": 30}}},
        {"acquire": {"tool": "qfk_system", "args": {"command": "lsof", "timeout": 180}}},
    ]

    assert _normalize_generated_timeouts(signals) == 3
    assert [signal["acquire"]["args"]["timeout"] for signal in signals] == [120, 120, 120, 180]


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
    assert rejected[0]["signal"]["acquire"]["args"]["timeout"] == 120
    assert "未被任何下游信号消费" in rejected[0]["reason"]


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
