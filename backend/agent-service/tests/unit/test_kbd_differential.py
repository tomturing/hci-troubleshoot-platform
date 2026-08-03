from unittest.mock import MagicMock

from app.adapters.agents.htp.kbd_differential import KBDDiagnostic
from app.tools.qfk.handlers import SystemHandler
from app.tools.qfk.signal import BackendSignal


def _diag() -> KBDDiagnostic:
    return KBDDiagnostic(MagicMock(), MagicMock())


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
    assert SystemHandler().build_commands(ps) == ["acli --timeout 30 system ps -p '{{PID}}' -o cmd="]
