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
    bridge_exec_msg,
    bridge_ssh_connect_msg,
    build_category_selection,
    build_exec_result_payload,
    conclusion_ungrounded_numbers,
    evaluate_invariants,
    extract_choice_options,
    extract_user_questions,
    find_interactive_request,
    guard_metric_fired,
    is_variable_request,
    iter_sse_events,
    match_candidate_option,
    normalize_agent_exec_command,
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


# ─────────────────────────────────────────────────────────────────
# 真实采集链路（headless bridge relay）报文构造纯逻辑
# ─────────────────────────────────────────────────────────────────


def test_normalize_agent_exec_command_wire_shape():
    # 对齐 conversation-service push_agent_exec_command 的 event_data（camelCase）
    data = {
        "execId": "exec-1",
        "toolName": "acli_exec",
        "command": "acli storage asan disk list",
        "builtCommand": "acli --formatter json storage asan disk list",
        "container": "host",
        "riskLevel": 1,
        "nodeIp": "172.16.100.13",
        "timeout": 20,
        "traceId": "trace-abc",
    }
    cmd = normalize_agent_exec_command(data)
    assert cmd["exec_id"] == "exec-1"
    # 优先用服务端拼装后的 builtCommand（与实际下发给 bridge 的一致）
    assert cmd["command"] == "acli --formatter json storage asan disk list"
    assert cmd["risk_level"] == 1
    assert cmd["node_ip"] == "172.16.100.13"
    assert cmd["timeout"] == 20


def test_bridge_ssh_connect_msg_from_connection():
    connection = {
        "support_id": "23821",
        "connection": {
            "host": "hci-diagnosis-lab-disk-badtrack",
            "port": 2222,
            "username": "sim",
            "auth_type": "password",
            "password": "lease-secret",
            "execution_mode": "synthetic",
            "test_run_id": "tr-1",
        },
    }
    msg = bridge_ssh_connect_msg(connection)
    assert msg["type"] == "ssh_connect"
    assert msg["case_id"] == "SIM-KBD-23821"
    assert msg["host"] == "hci-diagnosis-lab-disk-badtrack" and msg["port"] == 2222
    assert msg["password"] == "lease-secret" and msg["test_run_id"] == "tr-1"


def test_bridge_exec_msg_defaults_host_container():
    msg = bridge_exec_msg("acli system info", "exec-2", container=None, timeout=15)
    assert msg == {
        "type": "ssh_exec_process",
        "exec_id": "exec-2",
        "command": "acli system info",
        "container": "host",
        "timeout": 15,
    }


def test_build_exec_result_payload_success_has_sha_and_bytes():
    cmd = {"exec_id": "exec-3", "trace_id": "trace-xyz", "command": "acli system lsof"}
    result = {"stdout": "disk ok\n", "stderr": "", "exit_code": 0}
    payload = build_exec_result_payload(cmd, result)
    assert payload["exec_id"] == "exec-3"
    assert payload["exit_code"] == 0
    assert payload["stdout"] == "disk ok\n"
    assert payload["output"] == "disk ok\n"
    assert payload["stdout_bytes"] == len(b"disk ok\n")
    assert len(payload["stdout_sha256"]) == 64
    assert payload["trace_id"] == "trace-xyz"
    # 不携带 traceparent（避免不符合 W3C 正则被 422）
    assert "traceparent" not in payload


def test_build_exec_result_payload_failure_prefers_stderr():
    cmd = {"exec_id": "exec-4"}
    result = {"stdout": "", "stderr": "command not found", "exit_code": 127, "error_type": "command_not_found"}
    payload = build_exec_result_payload(cmd, result)
    assert payload["exit_code"] == 127
    assert payload["error_type"] == "command_not_found"
    assert "command not found" in payload["output"]


def test_evaluate_invariants_real_collection_green_and_red():
    base = dict(
        observed_category_code="硬件-025",
        expected_category_code="硬件-025",
        sop_document_hit_id=4,
        expected_sop_id=4,
        submitted_variables={},
        persisted_variables={"disk_dev": {"value": "/dev/sde"}, "asan_disks": {"value": "{...}"}},
        final_user_questions=[],
        resolved_variables=RESOLVED,
        declared_variables=DECLARED,
        guard_events=set(),
        expect_grounding_filter=False,
    )
    green = evaluate_invariants(**base, collected_exec_ids={"exec-a", "exec-b"}, expect_real_collection=True)
    assert green["failed"] == [], green["failed"]
    assert "V017_tool_call_collected" in [item.split(" — ")[0] for item in green["passed"]]
    # 验红：开了真采集期望却一个命令都没采到 → 必红（证明断言真的在守采集链）
    red = evaluate_invariants(**base, collected_exec_ids=set(), expect_real_collection=True)
    assert "V017_tool_call_collected" in [item.split(" — ")[0] for item in red["failed"]]


# ── S0 候选澄清（choice_options）纯逻辑：driver 必须能点选期望分类推进 S0→S1 ──

_CHOICE_EVENT = {
    "event": "metadata",
    "data": {
        "kind": "choice_options",
        "schemaVersion": 2,
        "requestId": "triage-b0569210",
        "options": [
            {"optionId": "硬件-024", "code": "硬件-024", "categoryName": "硬盘离线或不识别", "name": "硬件-024 硬盘离线或不识别"},
            {"optionId": "硬件-025", "code": "硬件-025", "categoryName": "硬盘坏道", "name": "硬件-025 硬盘坏道"},
            {"optionId": "硬件-026", "code": "硬件-026", "categoryName": "硬盘寿命到期", "name": "硬件-026 硬盘寿命到期"},
            {"optionId": "__none__", "name": "以上都不是（请补充症状描述）"},
        ],
    },
}


def test_extract_choice_options_from_typed_events():
    options = extract_choice_options([{"event": "message", "data": {"content": "hi"}}, _CHOICE_EVENT])
    assert [o["code"] for o in options if "code" in o] == ["硬件-024", "硬件-025", "硬件-026"]


def test_extract_choice_options_empty_when_absent():
    assert extract_choice_options([{"event": "message", "data": {"content": "hi"}}]) == []
    assert extract_choice_options([]) == []


def test_match_candidate_option_hits_expected_and_misses_none():
    options = extract_choice_options([_CHOICE_EVENT])
    hit = match_candidate_option(options, "硬件-025")
    assert hit is not None and hit["code"] == "硬件-025"
    # 验红：期望分类不在候选里 → 必须返回 None（绝不臆造匹配 __none__ 或错位项）
    assert match_candidate_option(options, "硬件-999") is None
    assert match_candidate_option(options, None) is None


def test_build_category_selection_uses_stable_code_contract():
    options = extract_choice_options([_CHOICE_EVENT])
    content, metadata = build_category_selection(match_candidate_option(options, "硬件-025"))
    assert metadata == {"selectedCategoryCode": "硬件-025"}
    assert "硬盘坏道" in content
