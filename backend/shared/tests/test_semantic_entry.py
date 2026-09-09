import json
from pathlib import Path

import pytest
from jsonschema import ValidationError
from shared.schemas.semantic_entry import lexical_profile_score, normalize_case_context, semantic_segment_weights
from shared.schemas.signal_schema import validate_kbd_publishable_signals_json


def _document(capability="executable"):
    return {
        "schema_version": 2,
        "signals": [
            {"id": "case_context", "role": "context", "acquire": {"tool": "qkv_case_context", "args": {}}},
            {
                "id": "system_check",
                "role": "must",
                "acquire": {"tool": "qfk_system", "args": {"command": "df -h"}},
                "match": {
                    "type": "keyword",
                    "pattern": "100%",
                    "expected": True,
                    "extract": {"type": "text", "rows": {"mode": "all"}},
                },
            },
        ],
        "semantic_entry_profile": {
            "schema_version": 1,
            "diagnosis_capability": capability,
            "canonical_symptoms": ["备份任务提示空间不足"],
            "positive_anchors": ["空间不足", "备份池"],
            "exclusion_anchors": ["网络超时"],
            "manual_evidence_request": ["请提供备份池容量截图"],
        },
        "verification_contract": {
            "evidence_policy": {
                "must": ["system_check"],
                "should": [],
                "exclude": [],
                "context": ["case_context"],
                "minimum_should": 0,
            }
        },
    }


def test_case_context_profile_can_be_published_without_strong_producer():
    validate_kbd_publishable_signals_json(_document())


def test_capability_gap_cannot_bypass_producer_gate():
    with pytest.raises(ValidationError, match="至少需要 1 条生产者信号"):
        validate_kbd_publishable_signals_json(_document("capability_gap"))


def test_profile_exclusion_is_fail_closed_and_does_not_infer_targets():
    profile = _document()["semantic_entry_profile"]
    score, positives, exclusions = lexical_profile_score(profile, "备份池空间不足，但网络超时")
    assert score == 0.0
    assert positives == ["空间不足", "备份池"]
    assert exclusions == ["网络超时"]


def test_case_context_is_split_deterministically_without_llm_or_target_inference():
    segments = normalize_case_context("虚拟机启动失败，页面提示“VM boot failed”，错误码 ERR-204，宿主机未知")

    assert segments["symptom"].startswith("虚拟机启动失败")
    assert "ERR-204" in segments["error_text"]
    assert "VM boot failed" in segments["error_text"]
    assert "虚拟机" in segments["object_operation"]
    assert "启动" in segments["object_operation"]
    assert "HOST" not in segments
    assert "VM_ID" not in segments

    weights = semantic_segment_weights(segments)
    assert set(weights) == {"symptom", "error_text", "object_operation"}
    assert sum(weights.values()) == pytest.approx(1.0)
    assert weights["symptom"] > weights["error_text"] > weights["object_operation"]


def test_case_context_allows_explicit_form_facts_but_never_guesses_them():
    segments = normalize_case_context(
        {
            "description": "创建虚拟机失败",
            "error_text": "镜像格式不支持",
            "object_type": "虚拟机",
            "operation": "创建",
        }
    )
    assert segments["error_text"] == "镜像格式不支持"
    assert segments["object_operation"] == "虚拟机 创建"


def test_hci_sim_semantic_entry_variants_cover_four_required_outcomes():
    fixture = Path(__file__).resolve().parents[3] / "hci_sim/testdata/sample-suites/diagnosis-signal-matrix-v1.json"
    scenarios = json.loads(fixture.read_text(encoding="utf-8"))["semantic_entry_scenarios"]
    assert set(scenarios) == {
        "executable_after_strong_no_match",
        "guidance_only_after_strong_no_match",
        "source_unavailable_fail_closed",
        "exclusion_anchor_rejected",
    }
    executable = scenarios["executable_after_strong_no_match"]
    score, positive, excluded = lexical_profile_score(executable["candidate"], executable["case_context"])
    assert score >= 0.25 and positive == executable["expected_positive_anchors"] and not excluded
    rejected = scenarios["exclusion_anchor_rejected"]
    score, _positive, excluded = lexical_profile_score(rejected["candidate"], rejected["case_context"])
    assert score == 0.0 and excluded == ["网络超时"]
    assert scenarios["guidance_only_after_strong_no_match"]["expected_offline_eligible"] is False
    assert scenarios["source_unavailable_fail_closed"]["expected_online_reason"] == "strong_producer_unavailable"
