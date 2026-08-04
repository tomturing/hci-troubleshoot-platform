import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from app.adapters.agents.htp.cdd import SignalOutcome
from app.adapters.agents.htp.kbd_differential import KBDDiagnostic, StepResult, _signal_requires_human
from app.tools.qfk.handlers import SystemHandler
from app.tools.qfk.signal import BackendSignal


def _diag() -> KBDDiagnostic:
    return KBDDiagnostic(MagicMock(), MagicMock())


def test_runtime_blocks_historical_solution_and_write_signals_but_not_read_only_checks():
    assert _signal_requires_human(
        {
            "acquire": {"tool": "qfk_system", "args": {"command": "sed"}},
            "orchestrate": {"phase": "solution"},
        }
    )
    assert _signal_requires_human(
        {
            "acquire": {"tool": "qfk_service", "args": {"command": "restart"}},
            "orchestrate": {"phase": "diagnostic"},
        }
    )
    assert not _signal_requires_human(
        {
            "acquire": {"tool": "qfk_system", "args": {"command": "cat"}},
            "orchestrate": {"phase": "diagnostic"},
        }
    )


def test_qfk_produces_use_new_text_and_json_extracts_atomically():
    diag = _diag()
    produces = [
        {"name": "USE_PERCENT", "type": "number", "extract": {"type": "text", "parser": "whitespace_table", "header": {"mode": "contains", "required": ["Use%"]}, "rows": {"mode": "indices", "basis": "data", "indices": [1]}, "columns": [{"key": "USE_PERCENT", "selector": {"by": "header", "name": "Use%"}, "value_mode": "number"}], "value_key": "USE_PERCENT"}},
        {"name": "STATUS", "type": "string", "extract": {"type": "json", "path": "data[0].status", "cardinality": "exactly_one", "source": "stderr", "value_mode": "string"}},
    ]
    ok, error = diag._fill_pool_from_qfk(produces, {"stdout": "Filesystem Use%\n/ 83%\n", "stderr": '{"data":[{"status":"alert"}]}'})
    assert (ok, error) == (True, None)
    assert diag._variable_pool == {"use_percent": 83.0, "status": "alert"}


def test_qfk_produces_reject_old_path_and_never_partially_write():
    diag = _diag()
    produces = [
        {"name": "FIRST", "type": "string", "extract": {"type": "text", "rows": {"mode": "all"}, "cardinality": "first"}},
        {"name": "OLD", "type": "string", "path": "data.0.value"},
    ]
    ok, error = diag._fill_pool_from_qfk(produces, {"stdout": "one\ntwo\n"})
    assert ok is False
    assert "新版 extract" in str(error)
    assert diag._variable_pool == {}


@pytest.mark.asyncio
async def test_qfk_ai_extract_produces_uses_grounded_value_and_preserves_atomic_write():
    registry = MagicMock()
    client = MagicMock()

    async def invoke(**_kwargs):
        return SimpleNamespace(
            content=json.dumps({"ok": True, "value": "192.168.100.55", "evidence_lines": [1]})
        )

    client.invoke.side_effect = invoke
    registry.get_client.return_value = client
    diag = KBDDiagnostic(registry, MagicMock())
    produces = [
        {
            "name": "DUP_IP",
            "type": "string",
            "extract": {
                "type": "text",
                "rows": {"mode": "all"},
                "cardinality": "all",
                "source": "stdout",
                "ai_extract": {"instruction": "提取其中的第一个 IP 地址"},
            },
        },
        {
            "name": "SECOND",
            "type": "string",
            "extract": {"type": "text", "rows": {"mode": "all"}, "cardinality": "first"},
        },
    ]
    outputs = {"stdout": "检测到IP，发生冲突，ip=192.168.100.55\nsecond\n"}

    ai_values, ai_error = await diag._extract_ai_values_from_qfk(produces, outputs)
    ok, extract_error = diag._fill_pool_from_qfk(produces, outputs, ai_values=ai_values)

    assert (ai_error, ok, extract_error) == (None, True, None)
    assert diag._variable_pool == {"dup_ip": "192.168.100.55", "second": "检测到IP，发生冲突，ip=192.168.100.55"}


@pytest.mark.asyncio
async def test_qfk_ai_extract_produces_failure_never_partially_writes_pool():
    registry = MagicMock()
    client = MagicMock()

    async def invoke(**_kwargs):
        return SimpleNamespace(
            content=json.dumps({"ok": True, "value": "192.168.100.99", "evidence_lines": [1]})
        )

    client.invoke.side_effect = invoke
    registry.get_client.return_value = client
    diag = KBDDiagnostic(registry, MagicMock())
    produces = [
        {
            "name": "SAFE",
            "type": "string",
            "extract": {"type": "text", "rows": {"mode": "all"}, "cardinality": "first"},
        },
        {
            "name": "DUP_IP",
            "type": "string",
            "extract": {
                "type": "text",
                "rows": {"mode": "all"},
                "cardinality": "all",
                "source": "stdout",
                "ai_extract": {"instruction": "提取其中的第一个 IP 地址"},
            },
        },
    ]

    ai_values, error = await diag._extract_ai_values_from_qfk(
        produces,
        {"stdout": "检测到IP，发生冲突，ip=192.168.100.55\n"},
    )

    assert ai_values == {}
    assert "QFK_AI_EXTRACT_UNGROUNDED" in str(error)
    assert diag._variable_pool == {}


def test_qfk_ai_matcher_value_is_exposed_in_tool_result_and_user_evidence():
    step = StepResult(
        tool_name="qfk_system",
        tool_args={"instruction": "检查 IP 冲突"},
        raw_output="检测到IP，发生冲突，ip=192.168.100.55",
        error=None,
        outcome=SignalOutcome.SATISFIED,
        ai_value="192.168.100.55",
    )

    metadata = KBDDiagnostic._tool_result_metadata(step)
    report = KBDDiagnostic._format_step_evidence(step)

    assert metadata["ai_value"] == "192.168.100.55"
    assert "AI 提取值" in report
    assert "192.168.100.55" in report


def test_state_matching_is_exact_after_json_extract():
    diag = _diag()
    matcher = {"type": "state", "pattern": "running", "expected": True, "extract": {"type": "json", "path": "status", "cardinality": "exactly_one", "value_mode": "string"}}
    assert diag._evaluate_matcher(matcher, '{"status":"running"}') is True
    assert diag._evaluate_matcher(matcher, '{"status":"running-extra"}') is False


def test_kbd27123_lsof_pid_then_ps_uses_canonical_argv_and_precise_process_identity():
    diag = _diag()
    produces = [{
        "name": "PID",
        "type": "integer",
        "extract": {
            "type": "text",
            "parser": "whitespace_table",
            "rows": {"mode": "keywords", "include": ["18864231143"], "exclude": [], "include_mode": "all", "case_sensitive": True},
            "columns": [{"key": "PID", "selector": {"by": "index", "index": 2}, "value_mode": "integer"}],
            "value_key": "PID",
            "cardinality": "first",
            "source": "stdout",
        },
    }]
    stdout = "flock      8369 root /18864231143.vm/vm-disk-2.qcow2\nsleep      8370 root /18864231143.vm/vm-disk-2.qcow2\n"

    ok, error = diag._fill_pool_from_qfk(produces, {"stdout": stdout})

    assert (ok, error) == (True, None)
    assert diag._variable_pool["pid"] == 8369
    ps = BackendSignal(namespace="system", command="ps -p {{PID}} -o cmd=", matcher=None)
    assert ps.command == "ps"
    assert ps.command_args == ["-p", "{{PID}}", "-o", "cmd="]
    assert SystemHandler().build_commands(ps) == ["acli --timeout 120 system ps -p '{{PID}}' -o cmd="]
