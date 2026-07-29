"""本地已有真实案例的 Gold Signal/Contract 必须通过发布 Schema 和运行时 Compiler。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from app.adapters.agents.htp.cdd import (
    SignalEvaluation,
    SignalOutcome,
    compile_signal_plan,
    replay_evaluations,
)
from app.adapters.agents.htp.kbd_model import KBD
from shared.schemas.signal_schema import validate_signals_json

REPO_ROOT = Path(__file__).resolve().parents[4]
GOLDEN_ROOT = REPO_ROOT / "tests" / "golden" / "kbd_cases"
MANIFEST = json.loads((GOLDEN_ROOT / "manifest.json").read_text(encoding="utf-8"))
GOLD_CASES = [item for item in MANIFEST["cases"] if item.get("annotation_status") == "gold"]


def _load_and_compile(case):
    gold = json.loads((GOLDEN_ROOT / case["gold"]).read_text(encoding="utf-8"))
    document = gold["signals_document"]
    validate_signals_json(document)
    candidate = KBD(
        id=case["support_id"],
        support_id=case["support_id"],
        category_id="golden",
        signals=document["signals"],
        verification_contract=document["verification_contract"],
    )
    return gold, compile_signal_plan([candidate], snapshot_id="golden-cases")


@pytest.mark.parametrize("case", GOLD_CASES, ids=lambda item: item["support_id"])
def test_gold_contract_compiles_with_runtime_handlers(case):
    _, plan = _load_and_compile(case)

    assert case["support_id"] not in plan.compile_errors, plan.compile_errors
    assert plan.acquisitions


@pytest.mark.parametrize("case", GOLD_CASES, ids=lambda item: item["support_id"])
def test_gold_replays_positive_negative_unknown_and_error_verdicts(case):
    """每条 Contract Gold 都必须证明支持、反证、未知和执行错误不会混淆。"""
    gold, plan = _load_and_compile(case)
    refs = {
        ref.signal_id: ref
        for ref in plan.signals.values()
        if ref.kbd_id == case["support_id"]
    }
    scenarios = gold.get("replay_scenarios") or []

    assert {item["id"] for item in scenarios} == {
        "positive",
        "strong_negative",
        "unknown",
        "error",
    }
    for scenario in scenarios:
        assert set(scenario["outcomes"]) == set(refs)
        evaluations = [
            SignalEvaluation(
                evaluation_id=f'{case["support_id"]}:{scenario["id"]}:eval:{index}',
                signal_ref_id=refs[signal_id].ref_id,
                exec_id=f'{case["support_id"]}:{scenario["id"]}:exec:{index}',
                outcome=SignalOutcome(outcome),
            )
            for index, (signal_id, outcome) in enumerate(
                scenario["outcomes"].items(),
                start=1,
            )
        ]

        assessments, coverage = replay_evaluations(
            plan,
            evaluations,
            gold["replay_environment"],
        )
        assessment = assessments[case["support_id"]]
        row = next(item for item in coverage.candidates if item.kbd_id == case["support_id"])

        assert assessment.verdict.value == scenario["expected_verdict"]
        assert row.observation_ratio == 1.0
        if scenario["id"] == "positive":
            assert row.must_satisfied == row.must_total
            assert row.should_satisfied >= row.minimum_should
        if scenario["id"] in {"unknown", "error"}:
            assert row.unresolved_signal_ids
