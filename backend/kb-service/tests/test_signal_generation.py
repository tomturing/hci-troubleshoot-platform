"""Signal/Contract generation provenance and stale detection tests."""

from app.routes.extract_signals import _signals_to_v2
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
