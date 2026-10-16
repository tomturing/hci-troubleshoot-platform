"""硬盘坏道 Live E2E 纯逻辑断言层单测（无需 docker/LLM）。

覆盖：SSE 解析、交互请求识别、user_questions 落地性、结论无溯源数值、不变量汇总，
以及「破坏缝隙能验红」的反向用例（证明断言真的在守，不是空跑）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from evaluation.live_e2e.asserts import (  # noqa: E402
    conclusion_ungrounded_numbers,
    evaluate_invariants,
    extract_user_questions,
    find_interactive_request,
    guard_metric_fired,
    is_variable_request,
    iter_sse_events,
    normalize_interactive_request,
    parse_prometheus_counters,
    user_questions_grounded,
)

SCENARIO_PATH = ROOT / "evaluation" / "live_e2e" / "scenarios" / "disk_badtrack.json"
DECLARED = {"alert_logs", "node_ip", "asan_disks", "disk_dev", "smart_info", "check_meth", "date"}
RESOLVED = {"node_ip", "disk_dev"}


def test_scenario_json_valid_and_grounds_key_facts():
    spec = json.loads(SCENARIO_PATH.read_text(encoding="utf-8"))
    assert spec["expect"]["category_code"] == "硬件-025"
    assert spec["expect"]["sop_document_id"] == 4
    assert "disk_dev" in spec["variables"]["declared"]
    # disk_sn / disk_type 绝不能出现在声明集里（否则不是幻觉）
    assert "disk_sn" not in spec["variables"]["declared"]
    assert "disk_type" not in spec["variables"]["declared"]


def test_iter_sse_events_ignores_done_and_bad_json():
    lines = [
        "data: " + json.dumps({"content": "x"}),
        "data: [DONE]",
        "data: {not-json",
        "event: message",
        "",
        "data: " + json.dumps({"content": "y"}),
    ]
    events = iter_sse_events(lines)
    assert len(events) == 2
    assert events[0]["content"] == "x" and events[1]["content"] == "y"


def test_find_interactive_request_extracts_ids_and_variable():
    events = [
        {"content": "thinking"},
        {
            "kind": "interactive_request",
            "event": {
                "request_id": "req-1",
                "acp_session_id": "sess-1",
                "variable_name": "disk_dev",
            },
        },
    ]
    req = find_interactive_request(events)
    assert req is not None
    assert req["request_id"] == "req-1"
    assert req["acp_session_id"] == "sess-1"
    assert req["variable"] == "disk_dev"


def test_user_questions_grounded_drops_reask_and_undeclared():
    questions = [
        "请提交 disk_dev（已就绪）",  # 命中已就绪 → 丢弃
        "请提供 disk_sn 和 disk_type",  # 未声明 token → 丢弃
        "是否授权我重新采集 asan_disks",  # 已声明未就绪 → 保留
        "现场是否已做过备份",  # 无变量样 token → 保留
    ]
    kept, dropped = user_questions_grounded(questions, RESOLVED, DECLARED)
    assert "现场是否已做过备份" in kept and "是否授权我重新采集 asan_disks" in kept
    assert "请提交 disk_dev（已就绪）" in dropped
    assert any("disk_sn" in d for d in dropped)


def test_user_questions_grounded_guardrail_when_no_declared():
    # 无事实源护栏：declared 为空则整体不过滤，避免误删
    kept, dropped = user_questions_grounded(["请提交 disk_dev"], RESOLVED, set())
    assert dropped == [] and len(kept) == 1


def test_extract_user_questions_from_fenced_json():
    text = "分析：\n```json\n" + json.dumps({"user_questions": ["a", "b"], "next_state": "S2"}) + "\n```"
    assert extract_user_questions(text) == ["a", "b"]
    assert extract_user_questions("纯文本没有 JSON") == []


def test_conclusion_ungrounded_numbers_detects_fabricated():
    conclusion = "该盘已写入 8001563222016 字节，坏道率 0.25%。"
    evidence = "smartctl 输出：Reallocated_Sector_Ct = 12"
    bad = conclusion_ungrounded_numbers(conclusion, evidence)
    assert "8001563222016" in bad and "0.25" in bad


def test_evaluate_invariants_all_green_when_guards_hold():
    result = evaluate_invariants(
        observed_category_code="硬件-025",
        expected_category_code="硬件-025",
        sop_document_hit_id=4,
        expected_sop_id=4,
        submitted_variables={"disk_dev": "/dev/sde"},
        persisted_variables={"disk_dev": {"value": "/dev/sde"}},
        final_user_questions=["现场是否已做过备份"],  # 干净
        resolved_variables=RESOLVED,
        declared_variables=DECLARED,
        guard_events={"sop_user_questions_grounded_filter"},
        expect_grounding_filter=True,
    )
    assert result["failed"] == [], result["failed"]
    assert len(result["passed"]) == 5


def test_evaluate_invariants_red_when_filter_bypassed():
    # 「破坏缝隙验红」：若引擎过滤被绕过，脏 user_questions 直抵输出 + 守卫未触发 → 必红
    result = evaluate_invariants(
        observed_category_code="硬件-025",
        expected_category_code="硬件-025",
        sop_document_hit_id=4,
        expected_sop_id=4,
        submitted_variables={"disk_dev": "/dev/sde"},
        persisted_variables={"disk_dev": {"value": "/dev/sde"}},
        final_user_questions=["请正式提交 disk_dev", "请提供 disk_sn 与 disk_type"],
        resolved_variables=RESOLVED,
        declared_variables=DECLARED,
        guard_events=set(),
        expect_grounding_filter=True,
    )
    failed_names = [item.split(" — ")[0] for item in result["failed"]]
    assert "UserQuestions_grounded" in failed_names
    assert "Guard_user_questions_filter_fired" in failed_names


def test_normalize_interactive_request_wire_shape():
    # 真实 SSE data（camelCase + metadata.variable_name），对齐 conversation_service L919
    data = {
        "requestId": "req-9",
        "acpSessionId": "sess-9",
        "kind": "variable_input",
        "title": "填写变量：disk_dev",
        "metadata": {"variable_name": "disk_dev", "required": True},
    }
    req = normalize_interactive_request(data)
    assert req["request_id"] == "req-9"
    assert req["acp_session_id"] == "sess-9"
    assert req["variable"] == "disk_dev"
    assert is_variable_request(req) is True
    # 非变量卡（intent_selection）不应被当作变量请求
    assert is_variable_request(normalize_interactive_request({"kind": "intent_selection"})) is False


def test_guard_metric_fired_only_on_positive_delta():
    before = {'agent_hallucination_detected_total{hallucination_type="ungrounded_user_question"}': 3.0}
    after_same = dict(before)
    after_up = {'agent_hallucination_detected_total{hallucination_type="ungrounded_user_question"}': 5.0}
    key = 'agent_hallucination_detected_total{hallucination_type="ungrounded_user_question"}'
    assert guard_metric_fired(before, after_same, key) is False
    assert guard_metric_fired(before, after_up, key) is True


def test_parse_prometheus_counters_keeps_labels():
    text = (
        "# HELP x\n"
        "# TYPE x counter\n"
        'agent_hallucination_detected_total{hallucination_type="ungrounded_user_question"} 7\n'
        "broken line without value\n"
    )
    series = parse_prometheus_counters(text)
    assert series['agent_hallucination_detected_total{hallucination_type="ungrounded_user_question"}'] == 7.0
    assert all("#" not in k for k in series)
