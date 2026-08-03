from types import SimpleNamespace

import pytest
from app.tools.acli import executor as executor_module
from app.tools.acli.executor import ExecResult
from app.tools.qfk import engine
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
    delta = evaluate_matcher({"type": "delta", "operator": ">", "value": 40, "expected": True, "extract": extract}, output)
    assert delta.matched is True
    trend = evaluate_matcher({"type": "trend", "direction": "increasing", "value": 1, "minimum_samples": 2, "expected": True, "extract": extract}, output)
    assert trend.matched is True


def test_json_path_is_an_extract_option_not_a_matcher_type():
    matcher = {"type": "state", "pattern": "healthy", "expected": True, "extract": {"type": "json", "path": "data[0].status", "cardinality": "exactly_one", "source": "stdout", "value_mode": "string"}}
    assert evaluate_matcher(matcher, '{"data":[{"status":"healthy"}]}').matched is True
    assert evaluate_matcher({"type": "json_path", "expected": True, "extract": matcher["extract"]}, "{}").matched is None


def test_matcher_without_extract_fails_closed():
    result = evaluate_matcher({"type": "threshold", "operator": ">", "value": 1, "expected": True}, "value=2")
    assert result.matched is None
    assert "extract" in result.detail["error"]


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
