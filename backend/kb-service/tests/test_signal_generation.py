"""Signal/Contract generation provenance and stale detection tests."""

import asyncio
from types import SimpleNamespace

from app.routes import extract_signals
from app.routes.extract_signals import _persist_signals, _signals_to_v2
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
