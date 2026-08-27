import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from app.tools.acli import executor as executor_module
from app.tools.acli.executor import ExecResult
from app.tools.qfk import engine
from app.tools.qfk.handlers import LogKeywordHandler
from app.tools.qfk.matcher import evaluate_matcher
from app.tools.qfk.signal import BackendSignal


def _rows_all(*, value_mode="string") -> dict:
    return {"type": "text", "rows": {"mode": "all"}, "cardinality": "all", "source": "stdout", "value_mode": value_mode}


def test_keyword_regex_state_and_exists_consume_extract_before_matching():
    keyword = evaluate_matcher({"type": "keyword", "pattern": "needle", "mode": "or", "expected": True, "extract": {"type": "text", "rows": {"mode": "keywords", "include": ["keep"], "exclude": [], "include_mode": "all", "case_sensitive": True}, "cardinality": "all"}}, "skip needle\nkeep needle\n")
    assert keyword.matched is True
    regex = evaluate_matcher({"type": "regex", "pattern": r"^ready$", "expected": True, "extract": _rows_all()}, "ready\n")
    assert regex.matched is True
    state = evaluate_matcher({"type": "state", "pattern": "running", "expected": True, "extract": {"type": "text", "rows": {"mode": "indices", "basis": "physical", "indices": [2]}, "cardinality": "exactly_one"}}, "header\nrunning\n")
    assert state.matched is True
    exists = evaluate_matcher({"type": "exists", "expected": True, "extract": {"type": "text", "rows": {"mode": "keywords", "include": ["missing"], "exclude": [], "include_mode": "all", "case_sensitive": True}}}, "present\n")
    assert exists.matched is False


def test_threshold_delta_and_trend_use_explicit_numeric_column():
    extract = {
        "type": "text", "parser": "whitespace_table", "header": {"mode": "contains", "required": ["Use%"]},
        "rows": {"mode": "all"}, "columns": [{"key": "USE_PERCENT", "selector": {"by": "header", "name": "Use%"}, "value_mode": "number"}],
        "value_key": "USE_PERCENT", "cardinality": "all",
    }
    output = "Filesystem Use%\n/root 35%\n/sf/log 83%\n"
    threshold = evaluate_matcher({"type": "threshold", "aggregation": "max", "operator": ">=", "value": 80, "expected": True, "extract": extract}, output)
    assert threshold.matched is True
    above_threshold_expected_false = evaluate_matcher({"type": "threshold", "aggregation": "max", "operator": ">=", "value": 80, "expected": False, "extract": extract}, output)
    assert above_threshold_expected_false.matched is False
    delta = evaluate_matcher({"type": "delta", "operator": ">", "value": 40, "expected": True, "extract": extract}, output)
    assert delta.matched is True
    trend = evaluate_matcher({"type": "trend", "direction": "increasing", "value": 1, "minimum_samples": 2, "expected": True, "extract": extract}, output)
    assert trend.matched is True

    below_threshold_expected_false = evaluate_matcher({"type": "threshold", "aggregation": "max", "operator": ">", "value": 100, "expected": False, "extract": {"type": "text", "rows": {"mode": "all"}, "cardinality": "all", "source": "stdout", "value_mode": "number"}}, "99\n")
    assert below_threshold_expected_false.matched is True


def test_threshold_consumes_count_cardinality_with_selected_line_evidence():
    matcher = {
        "type": "threshold",
        "aggregation": "first_number",
        "operator": ">=",
        "value": 2,
        "expected": True,
        "extract": {
            "type": "text",
            "rows": {"mode": "keywords", "include": ["failed"], "exclude": [], "include_mode": "all"},
            "cardinality": "count",
            "source": "stdout",
            "value_mode": "integer",
        },
    }

    result = evaluate_matcher(matcher, "failed task A\nready task B\nfailed task C\n")

    assert result.matched is True
    assert result.detail["value"] == 2.0
    assert result.detail["extract"]["values"] == [2]
    assert result.detail["extract"]["selected_line_numbers"] == [1, 3]


def test_json_path_is_an_extract_option_not_a_matcher_type():
    matcher = {"type": "state", "pattern": "healthy", "expected": True, "extract": {"type": "json", "path": "data[0].status", "cardinality": "exactly_one", "source": "stdout", "value_mode": "string"}}
    assert evaluate_matcher(matcher, '{"data":[{"status":"healthy"}]}').matched is True
    assert evaluate_matcher({"type": "json_path", "expected": True, "extract": matcher["extract"]}, "{}").matched is None


def test_matcher_without_extract_fails_closed():
    result = evaluate_matcher({"type": "threshold", "operator": ">", "value": 1, "expected": True}, "value=2")
    assert result.matched is None
    assert "extract" in result.detail["error"]


@pytest.mark.asyncio
async def test_qfk_wrapper_emits_one_terminal_event_for_early_return(monkeypatch):
    audit_logger = MagicMock()
    monkeypatch.setattr(engine, "logger", audit_logger)
    monkeypatch.setattr(executor_module, "_executor", None)
    signal = BackendSignal(namespace="system", command="df", matcher=None)

    result = await engine.qfk_exec(
        signal,
        conversation_id="conv-observe",
        case_id="case-observe",
        signal_id="sig-observe",
        execution_mode="produce",
    )

    assert result.error is not None
    events = [call.kwargs.get("event") for call in audit_logger.info.call_args_list]
    assert events.count("qfk_engine_started") == 1
    assert events.count("qfk_engine_finished") == 1
    assert "qfk_engine_failed" not in events
    finished = next(call.kwargs for call in audit_logger.info.call_args_list if call.kwargs.get("event") == "qfk_engine_finished")
    assert finished["case_id"] == "case-observe"
    assert finished["signal_id"] == "sig-observe"
    assert finished["final_matched"] is None


@pytest.mark.asyncio
async def test_qfk_bridge_exception_keeps_stable_error_code(monkeypatch):
    audit_logger = MagicMock()

    class FailingExecutor:
        _redis = SimpleNamespace()

        async def execute(self, **_kwargs):
            raise RuntimeError("bridge unavailable")

    monkeypatch.setattr(engine, "logger", audit_logger)
    monkeypatch.setattr(executor_module, "_executor", FailingExecutor())
    signal = BackendSignal(namespace="system", command="df", matcher=None)

    result = await engine.qfk_exec(
        signal,
        conversation_id="conv-bridge-failed",
        execution_mode="produce",
    )

    assert result.error == "QFK_BRIDGE_EXECUTION_FAILED: bridge unavailable"
    finished = next(call.kwargs for call in audit_logger.info.call_args_list if call.kwargs.get("event") == "qfk_engine_finished")
    assert finished["error_code"] == "QFK_BRIDGE_EXECUTION_FAILED"
    assert finished["status"] == "failed"


def test_whitebox_vt_uses_end_day_directory_and_and_remains_a_backend_predicate():
    """日志命令只做 OR 预筛，最终 AND 必须由 matcher 在完整输出上判定。"""
    matcher = {
        "type": "keyword",
        "pattern": ["检测到IP", "冲突"],
        "mode": "and",
        "expected": True,
        "extract": _rows_all(),
    }
    signal = BackendSignal(
        namespace="log",
        file="sfvt_vtpdaemon.log",
        time_window="2026-08-04 10:11:12",
        matcher=matcher,
    )

    command = LogKeywordHandler().build_commands(signal)[0]

    assert "-E -k" in command
    assert "检测到IP|冲突" in command or "冲突|检测到IP" in command
    assert "-p /sf/log/4/vt" in command
    assert "-t '2026-08-04 10:11:12'" in command
    assert evaluate_matcher(matcher, "仅检测到IP\n").matched is False
    assert evaluate_matcher(matcher, "检测到IP，发生冲突\n").matched is True


@pytest.mark.parametrize("time_window", [None, "{{END}}"])
def test_whitebox_vt_falls_back_to_log_root_without_resolved_end(time_window):
    signal = BackendSignal(
        namespace="log",
        file="sfvt_vtpdaemon.log",
        time_window=time_window,
        matcher={"type": "keyword", "pattern": ["检测到IP", "冲突"], "mode": "and", "expected": True},
    )

    command = LogKeywordHandler().build_commands(signal)[0]

    assert "-p /sf/log" in command
    assert "/today" not in command
    assert "/vt" not in command


def test_legacy_whitebox_today_path_does_not_override_end_day_directory():
    signal = BackendSignal(
        namespace="log",
        file="sfvt_vtpdaemon.log",
        path="/sf/log/today",
        time_window="2026-08-04",
        matcher={"type": "keyword", "pattern": "冲突", "mode": "or", "expected": True},
    )

    command = LogKeywordHandler().build_commands(signal)[0]

    assert "-p /sf/log/4/vt" in command
    assert "/sf/log/today" not in command


def test_log_resolution_retry_falls_back_to_same_day_root_without_zero_padded_day():
    command = "acli log get -E -k x -f sfvt_vtpdaemon.log -p /sf/log/1/vt -t '2026-01-01 00:00:08'"
    resolution = {
        "candidates_tried": [
            "/sf/log/1/vt/sfvt_vtpdaemon.log",
            "/sf/log/1/sfvt_vtpdaemon.log",
        ]
    }

    commands = engine._expand_log_resolution_retries([command], resolution)

    assert commands == [command, command.replace("-p /sf/log/1/vt", "-p /sf/log/1")]
    assert all("/sf/log/01" not in item for item in commands)


@pytest.mark.asyncio
async def test_producer_mode_returns_complete_output_without_matcher(monkeypatch):
    """KBD 的 lsof -> PID producer 不能被 QFK_MATCHER_MISSING 短路。"""

    output = "flock      8369 root /18864231143.vm/vm-disk-2.qcow2\n"

    class FakeExecutor:
        _redis = SimpleNamespace()

        async def execute(self, **_kwargs):
            return ExecResult(
                stdout=output,
                stderr="",
                exit_code=0,
                command="acli --timeout 120 system lsof",
                node="172.28.25.4",
                duration_ms=22000,
                truncated=True,
                risk_level=1,
                exec_id="qfk-producer-test",
            )

    async def fake_complete_output(_result, _redis, *, source):
        assert source == "stdout"
        return output

    monkeypatch.setattr(executor_module, "_executor", FakeExecutor())
    monkeypatch.setattr(engine, "get_complete_output", fake_complete_output)
    signal = BackendSignal(namespace="system", command="lsof", matcher=None, timeout=120)

    result = await engine.qfk_exec(
        signal,
        conversation_id="producer-test",
        required_output_sources={"stdout"},
        execution_mode="produce",
    )

    assert result.error is None
    assert result.matched is True
    assert result.complete_outputs == {"stdout": output}
    assert "产出变量规则" in result.evidence
    assert result.execution_status == "succeeded"
    assert result.processing_status == "succeeded"
    assert result.output_mode == "produce"
    assert result.business_output_available is False


@pytest.mark.asyncio
async def test_command_failure_has_no_valid_business_false(monkeypatch):
    class FakeExecutor:
        _redis = SimpleNamespace()

        async def execute(self, **_kwargs):
            return ExecResult(
                stdout="",
                stderr="df: invalid option",
                exit_code=2,
                command="acli --timeout 60 system df",
                node="172.28.25.4",
                duration_ms=1,
                truncated=False,
                risk_level=1,
            )

    monkeypatch.setattr(executor_module, "_executor", FakeExecutor())
    signal = BackendSignal(
        namespace="system",
        command="df",
        matcher={
            "type": "exists",
            "expected": True,
            "extract": {"type": "text", "rows": {"mode": "all"}, "cardinality": "all", "source": "stdout"},
        },
    )

    result = await engine.qfk_exec(
        signal,
        conversation_id="command-failed",
    )

    assert result.matched is False  # 兼容字段，不能作为本次业务结果消费
    assert result.execution_status == "failed"
    assert result.processing_status == "not_started"
    assert result.business_output_available is False
    assert result.error and result.error.startswith("QFK_COMMAND_FAILED")


@pytest.mark.asyncio
async def test_producer_command_failure_preserves_output_mode_and_never_exposes_output(monkeypatch):
    class FakeExecutor:
        _redis = SimpleNamespace()

        async def execute(self, **_kwargs):
            return ExecResult(
                stdout="partial value",
                stderr="vm query failed",
                exit_code=2,
                command="acli vm list --formatter json",
                node="172.28.25.4",
                duration_ms=1,
                truncated=False,
                risk_level=1,
            )

    monkeypatch.setattr(executor_module, "_executor", FakeExecutor())
    signal = BackendSignal(namespace="vm", command="list --formatter json", matcher=None)

    result = await engine.qfk_exec(signal, conversation_id="producer-command-failed", execution_mode="produce")

    assert result.output_mode == "produce"
    assert result.execution_status == "failed"
    assert result.processing_status == "not_started"
    assert result.business_output_available is False
    assert result.error and result.error.startswith("QFK_COMMAND_FAILED")


@pytest.mark.asyncio
async def test_producer_mode_rejects_matcher_conflict_before_variable_extraction(monkeypatch):
    class FakeExecutor:
        _redis = SimpleNamespace()

        async def execute(self, **_kwargs):
            return ExecResult(
                stdout="ClwDRDBClient",
                stderr="",
                exit_code=0,
                command="acli system ps",
                node="172.28.25.4",
                duration_ms=1,
                truncated=False,
                risk_level=1,
            )

    monkeypatch.setattr(executor_module, "_executor", FakeExecutor())
    signal = BackendSignal(
        namespace="system",
        command="ps",
        matcher={"type": "keyword", "pattern": "ClwDRDBClient", "expected": True},
    )

    result = await engine.qfk_exec(signal, conversation_id="producer-conflict", execution_mode="produce")

    assert result.error == "QFK_PRODUCER_MATCH_CONFLICT: 产出变量模式不得同时配置 matcher"


@pytest.mark.asyncio
async def test_matcher_ai_extract_runs_only_after_deterministic_hit_and_records_grounded_value(monkeypatch):
    output = "检测到IP，但没有冲突\n检测到IP，发生冲突，ip=192.168.100.55\n"

    class FakeExecutor:
        _redis = SimpleNamespace()

        async def execute(self, **_kwargs):
            return ExecResult(
                stdout=output,
                stderr="",
                exit_code=0,
                command="acli --timeout 120 system ps",
                node="172.28.25.4",
                duration_ms=1,
                truncated=False,
                risk_level=1,
            )

    class FakeAIClient:
        async def invoke(self, **_kwargs):
            return SimpleNamespace(
                content=json.dumps({"ok": True, "value": "192.168.100.55", "evidence_lines": [2]})
            )

    monkeypatch.setattr(executor_module, "_executor", FakeExecutor())
    signal = BackendSignal(
        namespace="system",
        command="ps",
        matcher={
            "type": "keyword",
            "pattern": ["检测到IP", "冲突"],
            "mode": "and",
            "expected": True,
            "extract": {
                "type": "text",
                "rows": {"mode": "all"},
                "cardinality": "all",
                "source": "stdout",
                "ai_extract": {"instruction": "提取其中的第一个 IP 地址"},
            },
        },
    )

    result = await engine.qfk_exec(
        signal,
        conversation_id="matcher-ai-extract",
        required_output_sources={"stdout"},
        ai_client=FakeAIClient(),
    )

    assert result.matched is True
    assert result.ai_value == "192.168.100.55"
    assert "引用物理行: [2]" in result.evidence


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("matcher_type", "matcher_extra", "ai_value", "output", "expected"),
    [
        ("delta", {"operator": "==", "value": 0, "minimum_samples": 2}, [347688534016, 347688534016], "Completed 347688534016 of 347688534016 bytes\\n", True),
        ("delta", {"operator": "==", "value": 0, "minimum_samples": 2}, [347688534016, 347688534017], "Completed 347688534016 of 347688534017 bytes\\n", False),
        # threshold 现在也使用 array<number>，通过聚合转为单值
        ("threshold", {"operator": ">=", "value": 347688534016}, [347688534016], "Completed 347688534016 bytes\\n", True),
        ("trend", {"direction": "increasing", "value": 1, "minimum_samples": 3}, [1, 2, 3], "Samples 1 2 3\\n", True),
    ],
)
async def test_numeric_matcher_consumes_grounded_ai_values_before_deterministic_judgement(
    monkeypatch, matcher_type, matcher_extra, ai_value, output, expected
):

    class FakeExecutor:
        _redis = SimpleNamespace()

        async def execute(self, **_kwargs):
            return ExecResult(
                stdout=output,
                stderr="",
                exit_code=0,
                command="acli --timeout 120 system ps",
                node="172.28.25.4",
                duration_ms=1,
                truncated=False,
                risk_level=1,
                exec_id="qfk-numeric-ai",
            )

    class FakeAIClient:
        async def invoke(self, **_kwargs):
            return SimpleNamespace(content=json.dumps({"ok": True, "value": ai_value, "evidence_lines": [1]}))

    monkeypatch.setattr(executor_module, "_executor", FakeExecutor())
    signal = BackendSignal(
        namespace="system",
        command="ps",
        matcher={
            "type": matcher_type,
            "expected": True,
            "extract": {
                "type": "text",
                "rows": {"mode": "all"},
                "cardinality": "all",
                "source": "stdout",
                "ai_extract": {"instruction": "按日志出现顺序提取数值"},
            },
            **matcher_extra,
        },
    )

    result = await engine.qfk_exec(
        signal,
        conversation_id="numeric-ai",
        required_output_sources={"stdout"},
        ai_client=FakeAIClient(),
    )

    assert result.error is None
    assert result.matched is expected
    # 所有数值 matcher 现在都返回数组
    assert result.ai_value == [float(item) for item in ai_value]
    assert "value_source=ai_grounded" in result.evidence or "AI 提取" in result.evidence


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("matcher_type", "matcher_extra", "ai_value_str", "output", "expected"),
    [
        # 测试 LLM 返回逗号分隔字符串而非数组的情况（符合系统提示词要求）
        # 输出必须包含所有提取的值
        ("threshold", {"operator": ">=", "value": 0}, "0, 0, 10, 11", "unaligned: 0, invalid: 0\\nunaligned: 10, invalid: 11\\n", True),
        ("threshold", {"operator": ">=", "value": 100}, "0, 0", "unaligned: 0, invalid: 0\\n", False),
        ("delta", {"operator": "==", "value": 0, "minimum_samples": 2}, "100, 100", "Samples: 100, 100\\n", True),
        ("trend", {"direction": "increasing", "value": 1, "minimum_samples": 3}, "1, 2, 3", "Samples 1 2 3\\n", True),
    ],
)
async def test_array_number_accepts_comma_separated_string(
    monkeypatch, matcher_type, matcher_extra, ai_value_str, output, expected
):
    """测试 array<number> 类型兼容 LLM 返回的逗号分隔字符串格式。

    系统提示词要求 LLM 返回字符串格式的 value，但 array<number> 类型期望列表。
    修复后应自动将 "0, 1, 2" 解析为 [0.0, 1.0, 2.0]。
    """

    class FakeExecutor:
        _redis = SimpleNamespace()

        async def execute(self, **_kwargs):
            return ExecResult(
                stdout=output,
                stderr="",
                exit_code=0,
                command="acli --timeout 120 system ps",
                node="172.28.25.4",
                duration_ms=1,
                truncated=False,
                risk_level=1,
                exec_id="qfk-array-number-string",
            )

    class FakeAIClient:
        async def invoke(self, **_kwargs):
            # 返回字符串格式的 value（符合系统提示词要求）
            return SimpleNamespace(content=json.dumps({"ok": True, "value": ai_value_str, "evidence_lines": [1]}))

    monkeypatch.setattr(executor_module, "_executor", FakeExecutor())
    signal = BackendSignal(
        namespace="system",
        command="ps",
        matcher={
            "type": matcher_type,
            "expected": True,
            "extract": {
                "type": "text",
                "rows": {"mode": "all"},
                "cardinality": "all",
                "source": "stdout",
                "ai_extract": {"instruction": "按日志出现顺序提取数值"},
            },
            **matcher_extra,
        },
    )

    result = await engine.qfk_exec(
        signal,
        conversation_id="array-number-string-test",
        required_output_sources={"stdout"},
        ai_client=FakeAIClient(),
    )

    assert result.error is None, f"预期无错误，但得到: {result.error}"
    assert result.matched is expected
    # 验证解析后的数组格式
    expected_values = [float(p.strip()) for p in ai_value_str.split(",")]
    assert result.ai_value == expected_values


@pytest.mark.asyncio
async def test_time_skew_uses_ai_derive_then_threshold_range(monkeypatch):
    output = (
        "10.97.128.120: Wed Aug 26 11:05:24 CST 2026\n"
        "10.97.128.13: Wed Aug 26 11:00:24 CST 2026\n"
        "10.97.128.11: Wed Aug 26 11:00:26 CST 2026\n"
    )

    class FakeExecutor:
        _redis = SimpleNamespace()

        async def execute(self, **_kwargs):
            return ExecResult(
                stdout=output,
                stderr="",
                exit_code=0,
                command="acli system date",
                node="10.97.128.120",
                duration_ms=1,
                truncated=False,
                risk_level=1,
                exec_id="qfk-time-skew",
            )

    class FakeAIClient:
        async def invoke(self, **_kwargs):
            return SimpleNamespace(
                content=json.dumps(
                    {"status": "success", "output": [1787713524, 1787713224, 1787713226], "evidence": [
                        {"ref": "line:1", "quote": "10.97.128.120: Wed Aug 26 11:05:24 CST 2026"},
                        {"ref": "line:2", "quote": "10.97.128.13: Wed Aug 26 11:00:24 CST 2026"},
                        {"ref": "line:3", "quote": "10.97.128.11: Wed Aug 26 11:00:26 CST 2026"}], "reason": "计算时间差"}
                )
            )

    monkeypatch.setattr(executor_module, "_executor", FakeExecutor())
    signal = BackendSignal(
        namespace="system",
        command="date",
        matcher={
            "type": "threshold",
            "aggregation": "range",
            "operator": ">",
            "value": 2,
            "expected": True,
            "extract": {
                "type": "text",
                "rows": {"mode": "all"},
                "cardinality": "all",
                "source": "stdout",
                "ai_processing": {
                    "mode": "derive",
                    "instruction": "从每行识别主机系统时间",
                    "output_type": "array", "item_type": "number",
                },
            },
        },
    )

    result = await engine.qfk_exec(
        signal,
        conversation_id="time-skew",
        required_output_sources={"stdout"},
        ai_client=FakeAIClient(),
    )

    assert result.error is None
    assert result.matched is True
    assert "threshold/range" in result.evidence
    assert "300.0 > 2.0" in result.evidence


# ─── nonzero_exit_as_negative 只读探针容错模式 ────────────────────────────────


@pytest.mark.asyncio
async def test_nonzero_exit_as_negative_file_not_found_becomes_matched_false(monkeypatch):
    """只读探针容错核心用例：
    非 GPU 主机上 cat /sf/cfg/gpu_info.ini 返回 exit=1，
    声明 nonzero_exit_as_negative=True 时，应得到 matched=False（否定证据）
    而非 QFK_COMMAND_FAILED（执行异常），从而允许 KBD 门禁正常放行同分类主案例。
    """

    class FakeExecutor:
        _redis = SimpleNamespace()

        async def execute(self, **_kwargs):
            return ExecResult(
                stdout="",
                stderr="cat: /sf/cfg/gpu_info.ini: No such file or directory",
                exit_code=1,
                command="acli --timeout 60 system cat /sf/cfg/gpu_info.ini",
                node="172.28.25.4",
                duration_ms=53,
                truncated=False,
                risk_level=1,
                exec_id="qfk-nonzero-test-001",
            )

    async def fake_complete_output(_result, _redis, *, source):
        return ""

    monkeypatch.setattr(executor_module, "_executor", FakeExecutor())
    monkeypatch.setattr(engine, "get_complete_output", fake_complete_output)
    signal = BackendSignal(
        namespace="system",
        command="cat",
        command_args=["/sf/cfg/gpu_info.ini"],
        instruction="检查GPU配置文件中是否存在gpu_type字段",
        matcher={
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
        nonzero_exit_as_negative=True,
    )

    result = await engine.qfk_exec(
        signal,
        conversation_id="test-nonzero-001",
        required_output_sources={"stdout"},
    )

    # 核心断言：不是 QFK_COMMAND_FAILED，而是正常的业务否定结论
    assert result.error is None, f"不应产生 error，但收到: {result.error}"
    assert result.matched is False
    assert result.execution_status == "succeeded"
    assert result.processing_status == "succeeded"
    assert result.business_output_available is True


@pytest.mark.asyncio
async def test_nonzero_exit_as_negative_with_terminal_sentinel_still_fails(monkeypatch):
    """二次安全门验证：
    即便声明了 nonzero_exit_as_negative=True，
    若 stderr 包含终端故障哨兵（如"SSH 会话不存在"），
    应仍然报告 QFK_TERMINAL_FAILURE，不被容错模式绕过。
    """

    class FakeExecutor:
        _redis = SimpleNamespace()

        async def execute(self, **_kwargs):
            return ExecResult(
                stdout="",
                stderr="SSH 会话不存在，需先 ssh_connect",
                exit_code=1,
                command="acli --timeout 60 system cat /sf/cfg/gpu_info.ini",
                node="172.28.25.4",
                duration_ms=5,
                truncated=False,
                risk_level=1,
                exec_id="qfk-nonzero-test-002",
            )

    monkeypatch.setattr(executor_module, "_executor", FakeExecutor())
    signal = BackendSignal(
        namespace="system",
        command="cat",
        command_args=["/sf/cfg/gpu_info.ini"],
        instruction="检查GPU配置文件中是否存在gpu_type字段",
        matcher={
            "type": "keyword",
            "pattern": "gpu_type",
            "mode": "or",
            "expected": True,
            "extract": {"type": "text", "rows": {"mode": "all"}, "cardinality": "all", "source": "stdout"},
        },
        nonzero_exit_as_negative=True,
    )

    result = await engine.qfk_exec(signal, conversation_id="test-nonzero-002")

    # 二次安全门：哨兵存在，容错模式不起作用，仍然拦截为终端故障
    assert result.error is not None
    assert result.error.startswith("QFK_TERMINAL_FAILURE")
    assert result.execution_status == "failed"
    assert result.business_output_available is False


@pytest.mark.asyncio
async def test_auto_inference_enables_nonzero_as_negative_for_readonly_cat(monkeypatch):
    """零配置自动推导验证：
    存量 KBD 信号未声明 nonzero_exit_as_negative 时，由于 command="cat" 属于只读白名单，
    模型校验器自动推导 nonzero_exit_as_negative=True，
    在文件不存在 exit=1 时自动得到 matched=False 否定结论，彻底杜绝存量案例门禁死锁。
    """

    class FakeExecutor:
        _redis = SimpleNamespace()

        async def execute(self, **_kwargs):
            return ExecResult(
                stdout="",
                stderr="cat: /sf/cfg/gpu_info.ini: No such file or directory",
                exit_code=1,
                command="acli --timeout 60 system cat /sf/cfg/gpu_info.ini",
                node="172.28.25.4",
                duration_ms=53,
                truncated=False,
                risk_level=1,
                exec_id="qfk-nonzero-test-auto",
            )

    async def fake_complete_output(_result, _redis, *, source):
        return ""

    monkeypatch.setattr(executor_module, "_executor", FakeExecutor())
    monkeypatch.setattr(engine, "get_complete_output", fake_complete_output)
    # 模拟存量信号：完全不传 nonzero_exit_as_negative
    signal = BackendSignal(
        namespace="system",
        command="cat",
        command_args=["/sf/cfg/gpu_info.ini"],
        instruction="检查GPU配置文件中是否存在gpu_type字段",
        matcher={
            "type": "keyword",
            "pattern": "gpu_type",
            "mode": "or",
            "expected": True,
            "extract": {"type": "text", "rows": {"mode": "all"}, "cardinality": "all", "source": "stdout"},
        },
    )

    # 验证自动推导已生效
    assert signal.nonzero_exit_as_negative is True

    result = await engine.qfk_exec(
        signal,
        conversation_id="test-nonzero-auto",
        required_output_sources={"stdout"},
    )

    assert result.error is None
    assert result.matched is False
    assert result.execution_status == "succeeded"
    assert result.business_output_available is True
