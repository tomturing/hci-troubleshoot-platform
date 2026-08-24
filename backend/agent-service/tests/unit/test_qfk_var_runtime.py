"""qfk_var Agent 分流、原子写入和 outcome 映射测试。"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from app.adapters.agents.htp.kbd_differential import KBDDiagnostic
from shared.cdd import SignalOutcome


def _diagnostic() -> KBDDiagnostic:
    agent = KBDDiagnostic.__new__(KBDDiagnostic)
    agent._variable_pool = {}
    agent._variable_pool_priority = {}
    agent._conversation_id = "conversation-test"
    agent._case_id = "case-test"
    agent._assistant_type = "htp-agent"
    agent._ai_registry = MagicMock()
    agent._tool_executor = MagicMock()
    return agent


@pytest.mark.asyncio
async def test_qfk_var_derive_writes_one_output_atomically() -> None:
    agent = _diagnostic()
    evidence, error, matched, _ = await agent._execute_qfk_var(
        {
            "id": "sig_var",
            "acquire": {
                "tool": "qfk_var",
                "args": {
                    "schema_version": 1,
                    "mode": "derive",
                    "operation": "cast",
                    "input": "{{VALUE}}",
                    "value_type": "percentage",
                },
            },
                "orchestrate": {"requires": ["VALUE"], "produces": [{"name": "CURRENT", "type": "number"}]},
        },
        {"value": "91%"},
        session_id="session-test",
    )
    assert error is None
    assert matched is True
    assert evidence and agent._variable_pool["current"] == 91.0


@pytest.mark.asyncio
async def test_qfk_var_conflict_never_overwrites_existing_value() -> None:
    agent = _diagnostic()
    agent._variable_pool["current"] = 90.0
    _, error, _, _ = await agent._execute_qfk_var(
        {
            "id": "sig_var",
            "acquire": {
                "tool": "qfk_var",
                "args": {
                    "schema_version": 1,
                    "mode": "derive",
                    "operation": "cast",
                    "input": "{{VALUE}}",
                    "value_type": "percentage",
                },
            },
            "orchestrate": {"requires": ["VALUE"], "produces": [{"name": "CURRENT", "type": "number"}]},
        },
        {"value": "91%"},
        session_id="session-test",
    )
    assert error and error.startswith("QFK_VAR_CONFLICT")
    assert agent._variable_pool["current"] == 90.0


@pytest.mark.asyncio
async def test_qfk_var_ai_fallback_only_runs_for_missing_stable_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    agent = _diagnostic()
    calls: list[str] = []

    async def fake_extract(*args, **kwargs):
        calls.append(args[0])
        return SimpleNamespace(
            raw_value="vm-new-01",
            evidence_line_numbers=[1],
            evidence_lines=["新格式资源标识：vm-new-01"],
        )

    monkeypatch.setattr("app.tools.qfk.ai_extractor.extract_ai_value", fake_extract)
    signal = {
        "id": "sig_ai",
        "acquire": {
            "tool": "qfk_var",
            "args": {
                "schema_version": 1,
                "mode": "derive",
                "operation": "feature_extract",
                "input": "{{DESCRIPTION}}",
                "target_variable": "vm_name",
                "value_type": "string",
                "cardinality": "exactly_one",
                "fallback": {"type": "ai_extract", "instruction": "提取资源标识"},
            },
        },
        "orchestrate": {"requires": ["DESCRIPTION"], "produces": [{"name": "VM_NAME", "type": "string"}]},
    }
    _, error, matched, _ = await agent._execute_qfk_var(signal, {"description": "新格式资源标识：vm-new-01"}, session_id="session-test")
    assert error is None and matched is True
    assert calls == ["新格式资源标识：vm-new-01"]
    assert agent._variable_pool["vm_name"] == "vm-new-01"


@pytest.mark.asyncio
async def test_qfk_var_ai_fallback_does_not_replace_ambiguous_deterministic_candidates(monkeypatch: pytest.MonkeyPatch) -> None:
    agent = _diagnostic()
    called = False

    async def fake_extract(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("ambiguous 不得调用 AI")

    monkeypatch.setattr("app.tools.qfk.ai_extractor.extract_ai_value", fake_extract)
    signal = {
        "id": "sig_ambiguous",
        "acquire": {
            "tool": "qfk_var",
            "args": {
                "schema_version": 1, "mode": "derive", "operation": "feature_extract",
                "input": "{{DESCRIPTION}}", "target_variable": "vm_name",
                "value_type": "string", "cardinality": "exactly_one",
                "fallback": {"type": "ai_extract", "instruction": "提取虚拟机名称"},
            },
        },
        "orchestrate": {"requires": ["DESCRIPTION"], "produces": [{"name": "VM_NAME", "type": "string"}]},
    }
    _, error, matched, _ = await agent._execute_qfk_var(signal, {"description": "虚拟机（vm-a）和虚拟机（vm-b）"}, session_id="session-test")
    assert error and error.startswith("QFK_VAR_CARDINALITY_MISMATCH")
    assert matched is None and called is False


def test_qfk_var_errors_map_to_unknown_or_blocked_without_false_negative() -> None:
    agent = _diagnostic()
    signal = {"acquire": {"tool": "qfk_var", "args": {"on_error": "unknown"}}}
    assert agent._evaluate_signal_outcome(signal, None, "QFK_VAR_TYPE_MISMATCH: bad", None) is SignalOutcome.UNKNOWN
    assert agent._evaluate_signal_outcome(signal, None, "QFK_VAR_BLOCKED: missing", None) is SignalOutcome.BLOCKED
    assert (
        agent._evaluate_signal_outcome(signal, None, "QFK_VAR_CARDINALITY_MISMATCH: many", None)
        is SignalOutcome.UNKNOWN
    )
