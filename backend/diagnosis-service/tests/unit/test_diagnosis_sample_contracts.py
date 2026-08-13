"""五篇诊断样例的离线同步、资源生成和诊断语义回归。"""

from __future__ import annotations

import json
import re
from pathlib import Path

from app.services.offline_acquisition_compiler import compile_signal_acquisition
from app.services.offline_analysis_service import _evaluate_matcher
from shared.cdd import CandidateState, SignalOutcome, compile_signal_plan
from shared.cdd.candidate_reducer import initial_assessments, reduce_candidates
from shared.cdd.kbd_model import kbd_from_dict

REPO_ROOT = Path(__file__).resolve().parents[4]
SEED_PATH = REPO_ROOT / "database" / "seeds" / "04_kbd_diagnosis_samples.sql"
PROFILE_PATH = REPO_ROOT / "hci_sim" / "testdata" / "sample-suites" / "diagnosis-signal-matrix-v1.json"
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


def _matched_evidence(signal: dict) -> object:
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


def test_five_samples_compile_every_signal_into_offline_collectors():
    documents = _documents()
    assert {item["verification_contract"]["case_id"] for item in documents} == SAMPLE_IDS
    for document in documents:
        for signal in document["signals"]:
            compiled = compile_signal_acquisition(
                tool=signal["acquire"]["tool"],
                args=signal["acquire"]["args"],
                matcher=signal.get("match"),
                produces=(signal.get("orchestrate") or {}).get("produces") or [],
            )
            assert compiled.command_template.startswith("acli ")
            assert "{{" not in compiled.command_template
            assert compiled.resolution_status.value in {"verified", "needs_probe"}


def test_vm_sample_normalizes_global_formatter_and_inherits_command_version_gate():
    """vm 全局参数位置和 status get 最低版本必须在同步阶段固化。"""

    vm_document = next(
        item for item in _documents() if item["verification_contract"]["case_id"] == "SAMPLE-SIG-VM"
    )
    signals = {item["id"]: item for item in vm_document["signals"]}
    status = compile_signal_acquisition(
        tool="qfk_vm",
        args=signals["vm_status_must"]["acquire"]["args"],
        matcher=signals["vm_status_must"]["match"],
    )
    listing = compile_signal_acquisition(
        tool="qfk_vm",
        args=signals["vm_list_context"]["acquire"]["args"],
    )

    assert status.supported_product_versions == [">=6.12.0"]
    assert status.command_template == "acli --formatter {formatter} vm status get --vm-id {target_id}"
    assert status.parameters["formatter"] == "json"
    assert status.query_type == "json"
    assert listing.command_template == "acli --formatter {formatter} vm list"
    assert listing.parameters["formatter"] == "json"
    assert listing.query_type == "json"

    legacy_listing = compile_signal_acquisition(
        tool="qfk_vm",
        args={"command": "list", "command_args": ["--formatter", "json"]},
    )
    assert legacy_listing.command_template == "acli --formatter {formatter} vm list"


def test_five_samples_reach_supported_state_with_matching_offline_evidence():
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
        plan = compile_signal_plan([candidate], snapshot_id="offline-diagnosis-sample")
        assert plan.compile_errors == {}, {support_id: plan.compile_errors}
        assessments = initial_assessments(plan)
        assessment = assessments[candidate.id]
        for ref in plan.signals.values():
            matcher = ref.signal.get("match")
            if matcher is None:
                outcome = SignalOutcome.SATISFIED
            elif ref.evidence_role.value == "exclude":
                outcome = SignalOutcome.CONTRADICTED
            else:
                matched = _evaluate_matcher(matcher, _matched_evidence(ref.signal))
                assert matched is True, f"{support_id}/{ref.signal_id} 的离线 Matcher 未命中样例证据"
                outcome = SignalOutcome.SATISFIED
            assessment.signal_outcomes[ref.ref_id] = outcome
        reduce_candidates(plan, assessments, finalize=True)
        assert assessment.state is CandidateState.SUPPORTED, {support_id: assessment.reasons}


def test_diagnosis_lab_outputs_drive_real_matchers_in_both_directions():
    """场景画像的正/负输出必须真实通过共享 Matcher，而不只是字段齐全。"""

    lab_profile = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    documents = {item["verification_contract"]["case_id"]: item for item in _documents()}
    for support_id, case_profile in lab_profile["cases"].items():
        variables = {**lab_profile.get("variables", {}), **case_profile.get("variables", {})}

        def render(value: str, bindings: dict = variables, case_id: str = support_id) -> str:
            for name, replacement in bindings.items():
                value = value.replace("{{" + name + "}}", str(replacement))
            assert "{{" not in value, f"{case_id} 场景输出存在未解析变量: {value}"
            return value

        for signal in documents[support_id]["signals"]:
            matcher = signal.get("match")
            if matcher is None:
                continue
            outputs = case_profile["signals"][signal["id"]]
            positive = _evaluate_matcher(matcher, render(outputs["positive_output"]))
            negative = _evaluate_matcher(matcher, render(outputs["negative_output"]))
            if signal.get("role") == "exclude":
                assert positive is False, f"{support_id}/{signal['id']} 正常输出错误触发排除条件"
                assert negative is True, f"{support_id}/{signal['id']} 异常输出未触发排除条件"
            else:
                assert positive is True, f"{support_id}/{signal['id']} 正场景未命中 Matcher"
                assert negative is not True, f"{support_id}/{signal['id']} 负场景意外命中 Matcher"
