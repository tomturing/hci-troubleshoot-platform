import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from app.adapters.agents.htp.kbd_differential import KBDDiagnostic, StepResult, _signal_requires_human
from app.tools.qfk.handlers import SystemHandler
from app.tools.qfk.signal import BackendSignal
from shared.cdd import SignalOutcome


def _diag() -> KBDDiagnostic:
    return KBDDiagnostic(MagicMock(), MagicMock())


@pytest.mark.asyncio
async def test_sim_ssh_binds_logical_host_to_authoritative_managed_endpoint():
    diag = _diag()

    endpoint = await diag._resolve_host_ip(
        "SIM-HCI-NODE-01",
        node_ip="hci-sim.hci-sim-dev.svc",
        execution_mode="sim-ssh",
        session_id="simulation-session",
    )

    assert endpoint == "hci-sim.hci-sim-dev.svc"
    assert diag._host_ip_cache["SIM-HCI-NODE-01"] == endpoint


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


@pytest.mark.asyncio
async def test_qkv_output_processing_commits_raw_and_derived_values_atomically():
    diag = _diag()
    signal = {
        "acquire": {"tool": "qkv_alert", "args": {}},
        "orchestrate": {
            "produces": [{"name": "DESCRIPTION", "path": "description"}],
            "output_processing": [{
                "id": "vm",
                "mode": "derive",
                "input": "{{DESCRIPTION}}",
                "operation": "feature_extract",
                "target_variable": "VM_NAME",
                "feature": "vm_name",
            }],
        },
    }
    result = SimpleNamespace(
        values=[{"description": "虚拟机名称：vm-001"}],
        processing_applied=False,
    )

    await diag._fill_pool_from_qkv(signal, result)

    assert diag._variable_pool == {
        "description": "虚拟机名称：vm-001",
        "vm_name": "vm-001",
    }


@pytest.mark.asyncio
async def test_qkv_output_processing_multiple_records_commit_derived_list():
    diag = _diag()
    signal = {
        "acquire": {"tool": "qkv_alert", "args": {}},
        "orchestrate": {
            "produces": [{"name": "DESCRIPTION", "path": "description"}],
            "output_processing": [{
                "id": "vm",
                "mode": "derive",
                "input": "{{DESCRIPTION}}",
                "operation": "feature_extract",
                "target_variable": "VM_NAME",
                "feature": "vm_name",
            }],
        },
    }
    result = SimpleNamespace(
        values=[
            {"description": "虚拟机名称：vm-001"},
            {"description": "虚拟机名称：vm-002"},
        ],
        processing_applied=False,
    )

    await diag._fill_pool_from_qkv(signal, result)

    # 原 produces 变量沿用既有 QKV 语义取首条；后处理派生变量按多记录汇总。
    assert diag._variable_pool["description"] == "虚拟机名称：vm-001"
    assert diag._variable_pool["vm_name"] == ["vm-001", "vm-002"]


def test_numeric_matcher_threshold_resolves_variable_before_comparison():
    diag = _diag()
    matcher = {
        "type": "threshold",
        "operator": ">",
        "value": "{{THRESHOLD}}",
        "expected": True,
        "extract": {"type": "text", "rows": {"mode": "all"}, "cardinality": "first", "value_mode": "number"},
    }

    resolved = diag._resolve_template_value(matcher, {"THRESHOLD": 80})
    assert resolved["value"] == "80"
    assert diag._evaluate_matcher(resolved, "81\n") is True
    assert diag._evaluate_matcher(diag._resolve_template_value(matcher, {}), "81\n") is None


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
    assert SystemHandler().build_commands(ps) == ["acli --timeout 60 system ps -p '{{PID}}' -o cmd="]


def test_diagnostic_report_shows_exclusion_reasons_for_unconfirmed_candidates():
    """候选被排除时报告必须携带原因（契约过期/编译错误/scope 拦截），
    否则「无可执行证据」会把数据与契约问题吞成同一文案，现场无法定位。"""
    from app.adapters.agents.htp.kbd_model import KBD

    kbd = KBD(id="127", support_id="23821", name="[HCI] 690虚拟机迁移存储位置，一直卡在9%")
    report = KBDDiagnostic._build_diagnostic_report(
        [],
        [],
        evaluated_kbds=[kbd],
        exclusion_reasons={
            "127": [
                "expert publish tool contract revision is stale",
                "scope UNKNOWN: missing environment.version",
            ]
        },
    )

    assert "参考案例 23821" in report
    assert "（未确认：expert publish tool contract revision is stale；scope UNKNOWN: missing environment.version）" in report
    assert "无可执行证据" in report


def test_diagnostic_report_keeps_legacy_suffix_without_exclusion_reasons():
    """未提供排除原因时保持原「（未确认）」文案，向后兼容。"""
    from app.adapters.agents.htp.kbd_model import KBD

    kbd = KBD(id="127", support_id="23821", name="x")
    report = KBDDiagnostic._build_diagnostic_report([], [], evaluated_kbds=[kbd])

    assert "（未确认）" in report
    assert "（未确认：" not in report


def test_partial_report_keeps_supported_reference_case_without_root_cause():
    """一篇 KBD 已命中、另一篇未决时，必须展示命中案例但不能输出根因。"""
    from app.adapters.agents.htp.kbd_model import KBD

    kbd = KBD(
        id="1",
        support_id="27123",
        name="虚拟机镜像忙",
        root_cause="镜像被其他进程占用",
        solution="结束占用进程",
    )
    step = StepResult(
        tool_name="qkv_task",
        tool_args={"instruction": "获取虚拟机开机失败任务详情"},
        raw_output='{"data": [{"type": "启动虚拟机", "status": "failed"}]}',
        error=None,
        kbd_id="1",
        signal_id="sig_001",
        outcome=SignalOutcome.SATISFIED,
    )

    report = KBDDiagnostic._build_partial_supported_summary([kbd], [step])

    assert "已命中参考案例" in report
    assert "参考案例 27123 - 虚拟机镜像忙" in report
    assert "必需关键信号已全部命中" in report
    assert "镜像被其他进程占用" not in report
    assert "结束占用进程" not in report
