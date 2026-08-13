"""五篇诊断样例的在线 Agent 回归门禁。"""

from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import MagicMock

from app.adapters.agents.htp.kbd_differential import KBDDiagnostic, _tool_contract_checker
from shared.cdd import CandidateState, SignalOutcome, compile_signal_plan
from shared.cdd.candidate_reducer import initial_assessments, reduce_candidates
from shared.cdd.kbd_model import kbd_from_dict
from shared.resolution.review import SignalReviewFeature, review_signal_document

REPO_ROOT = Path(__file__).resolve().parents[4]
SEED_PATH = REPO_ROOT / "database" / "seeds" / "04_kbd_diagnosis_samples.sql"
SAMPLE_IDS = {
    "SAMPLE-SIG-VM",
    "SAMPLE-SIG-CORE",
    "SAMPLE-SIG-LOG",
    "SAMPLE-SIG-NET-STO",
    "SAMPLE-SIG-HW-PLT",
}


def _documents() -> list[dict]:
    sql = SEED_PATH.read_text(encoding="utf-8")
    return [
        json.loads(payload) for payload in re.findall(r"\$signals\$\s*(\{.*?\})\s*\$signals\$::jsonb", sql, re.DOTALL)
    ]


def _matched_output(signal: dict) -> str:
    matcher = signal.get("match") or {}
    matcher_type = matcher.get("type")
    if matcher_type == "keyword":
        pattern = matcher.get("pattern")
        return " ".join(pattern if isinstance(pattern, list) else [str(pattern)])
    if matcher_type == "regex":
        return "eth0    link up"
    if matcher_type == "state":
        return "running"
    if matcher_type == "threshold":
        return "Filesystem Use%\n/sf/log 83%\n"
    if matcher_type == "delta":
        if signal["acquire"]["tool"] == "qfk_storage":
            return "Volume IOPS\nsample-volume 10\nsample-volume 240\n"
        return "Metric Value\nrx_dropped 0\nrx_dropped 120\n"
    if matcher_type == "trend":
        if signal["acquire"]["tool"] == "qfk_hardware":
            return "Metric Value\ngpu_temperature 40\ngpu_temperature 45\ngpu_temperature 52\n"
        return "Metric Value\ntx_dropped 1\ntx_dropped 3\ntx_dropped 8\n"
    if matcher_type == "exists":
        return '{"data":[{"id":"sample"}]}'
    return "collected"


def test_five_samples_pass_publish_review_and_online_cdd_compilation():
    documents = _documents()
    assert {item["verification_contract"]["case_id"] for item in documents} == SAMPLE_IDS
    for index, document in enumerate(documents, start=1):
        support_id = document["verification_contract"]["case_id"]
        review = review_signal_document(document, feature=SignalReviewFeature.PUBLISH)
        assert not review.blocked, review.model_dump(mode="json")
        candidate = kbd_from_dict(
            {
                "id": str(index),
                "support_id": support_id,
                "name": support_id,
                "category_id": support_id,
                "signals": document,
                "resource_revision": {"revision": 1},
            }
        )
        plan = compile_signal_plan(
            [candidate],
            snapshot_id="diagnosis-sample-preflight",
            tool_contract_checker=_tool_contract_checker,
        )
        assert plan.compile_errors == {}, {support_id: plan.compile_errors}
        assert len(plan.signals) == len(document["signals"])
        assert plan.acquisitions


def test_five_samples_reach_supported_state_with_online_agent_matchers():
    """在线 Agent 的真实 Matcher 与 CDD 归约必须逐篇得出 SUPPORTED。"""

    diagnostic = KBDDiagnostic(MagicMock(), MagicMock())
    for index, document in enumerate(_documents(), start=1):
        support_id = document["verification_contract"]["case_id"]
        candidate = kbd_from_dict(
            {
                "id": str(index),
                "support_id": support_id,
                "name": support_id,
                "category_id": support_id,
                "signals": document,
                "resource_revision": {"revision": 1},
            }
        )
        plan = compile_signal_plan(
            [candidate],
            snapshot_id="diagnosis-sample-online-evidence",
            tool_contract_checker=_tool_contract_checker,
        )
        assessments = initial_assessments(plan)
        assessment = assessments[candidate.id]
        for ref in plan.signals.values():
            matcher = ref.signal.get("match")
            if matcher is None:
                outcome = SignalOutcome.SATISFIED
            elif ref.evidence_role.value == "exclude":
                outcome = SignalOutcome.CONTRADICTED
            else:
                assert diagnostic._evaluate_matcher(matcher, _matched_output(ref.signal)) is True
                outcome = SignalOutcome.SATISFIED
            assessment.signal_outcomes[ref.ref_id] = outcome
        reduce_candidates(plan, assessments, finalize=True)
        assert assessment.state is CandidateState.SUPPORTED, {support_id: assessment.reasons}
