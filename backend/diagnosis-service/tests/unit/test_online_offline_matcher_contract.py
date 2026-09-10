"""在线 Shared Matcher 与离线证据适配的 Golden Contract（黄金契约）回归。"""

from unittest.mock import MagicMock

import pytest
from app.services.offline_analysis_service import (
    OfflineAnalysisService,
    _evaluate_matcher,
    _evaluate_offline_producer,
    _extract_offline_producer_values,
    _flatten_text,
)
from shared.cdd import ProducedVariable
from shared.signals.matcher import evaluate_matcher

TEXT_ALL = {
    "type": "text",
    "rows": {"mode": "all"},
    "cardinality": "all",
    "source": "stdout",
    "value_mode": "string",
}
NUMERIC_TABLE = {
    "type": "text",
    "parser": "whitespace_table",
    "header": {"mode": "contains", "required": ["Use%"]},
    "rows": {"mode": "all"},
    "columns": [
        {
            "key": "USE_PERCENT",
            "selector": {"by": "header", "name": "Use%"},
            "value_mode": "number",
        }
    ],
    "value_key": "USE_PERCENT",
    "cardinality": "all",
}
TABLE_OUTPUT = "Filesystem Use%\n/root 35%\n/sf/log 83%\n"


GOLDEN_CASES = [
    ({"type": "keyword", "pattern": "busy", "mode": "or", "expected": True, "extract": TEXT_ALL}, "disk busy", True),
    ({"type": "keyword", "pattern": "busy", "mode": "or", "expected": True, "extract": TEXT_ALL}, "disk idle", False),
    (
        {"type": "keyword", "pattern": ["disk", "busy"], "mode": "and", "expected": True, "extract": TEXT_ALL},
        "disk busy",
        True,
    ),
    ({"type": "keyword", "pattern": "error", "mode": "not", "expected": True, "extract": TEXT_ALL}, "healthy", True),
    ({"type": "keyword", "pattern": "error", "mode": "or", "expected": False, "extract": TEXT_ALL}, "error", False),
    ({"type": "regex", "pattern": r"^ready$", "expected": True, "extract": TEXT_ALL}, "ready\n", True),
    ({"type": "regex", "pattern": r"^ready$", "expected": True, "extract": TEXT_ALL}, "not-ready\n", False),
    ({"type": "regex", "pattern": r"node-\d+", "expected": False, "extract": TEXT_ALL}, "node-12\n", False),
    (
        {
            "type": "state",
            "pattern": "running",
            "expected": True,
            "extract": {"type": "json", "path": "status", "cardinality": "exactly_one", "value_mode": "string"},
        },
        {"status": "running"},
        True,
    ),
    (
        {
            "type": "state",
            "pattern": "running",
            "expected": True,
            "extract": {"type": "json", "path": "status", "cardinality": "exactly_one", "value_mode": "string"},
        },
        {"status": "stopped"},
        False,
    ),
    (
        {
            "type": "state",
            "pattern": ["ready", "running"],
            "expected": True,
            "extract": {"type": "json", "path": "status", "cardinality": "exactly_one", "value_mode": "string"},
        },
        {"status": "ready"},
        True,
    ),
    (
        {
            "type": "exists",
            "expected": True,
            "extract": {"type": "text", "rows": {"mode": "keywords", "include": ["present"]}},
        },
        "present",
        True,
    ),
    (
        {
            "type": "exists",
            "expected": True,
            "extract": {"type": "text", "rows": {"mode": "keywords", "include": ["missing"]}},
        },
        "present",
        False,
    ),
    (
        {
            "type": "threshold",
            "aggregation": "max",
            "operator": ">=",
            "value": 80,
            "expected": True,
            "extract": NUMERIC_TABLE,
        },
        TABLE_OUTPUT,
        True,
    ),
    (
        {
            "type": "threshold",
            "aggregation": "max",
            "operator": ">",
            "value": 90,
            "expected": True,
            "extract": NUMERIC_TABLE,
        },
        TABLE_OUTPUT,
        False,
    ),
    (
        {
            "type": "threshold",
            "aggregation": "min",
            "operator": "<",
            "value": 40,
            "expected": True,
            "extract": NUMERIC_TABLE,
        },
        TABLE_OUTPUT,
        True,
    ),
    ({"type": "delta", "operator": ">", "value": 40, "expected": True, "extract": NUMERIC_TABLE}, TABLE_OUTPUT, True),
    ({"type": "delta", "operator": ">", "value": 60, "expected": True, "extract": NUMERIC_TABLE}, TABLE_OUTPUT, False),
    (
        {
            "type": "trend",
            "direction": "increasing",
            "value": 1,
            "minimum_samples": 2,
            "expected": True,
            "extract": NUMERIC_TABLE,
        },
        TABLE_OUTPUT,
        True,
    ),
    (
        {
            "type": "trend",
            "direction": "decreasing",
            "value": 1,
            "minimum_samples": 2,
            "expected": True,
            "extract": NUMERIC_TABLE,
        },
        TABLE_OUTPUT,
        False,
    ),
]


@pytest.mark.parametrize(("matcher", "evidence", "expected"), GOLDEN_CASES)
def test_online_and_offline_matcher_contracts_are_identical(matcher, evidence, expected):
    """20 组代表性证据必须在 Shared 在线语义与离线适配中得到相同结果。"""

    online = evaluate_matcher(matcher, _flatten_text(evidence)).matched
    offline = _evaluate_matcher(matcher, evidence)

    assert online is expected
    assert offline is expected


def test_offline_qkv_producer_requires_declared_values_and_processing_to_pass():
    """离线 available 制品不能绕过 QKV 的 produces 与断言处理链。"""
    service = OfflineAnalysisService(MagicMock())
    signal = {
        "id": "task-description",
        "acquire": {"tool": "qkv_task", "args": {}},
        "match": None,
        "orchestrate": {
            "produces": [{"name": "DESCRIPTION", "path": "description"}],
            "output_processing": [
                {"mode": "assert", "input": "{{DESCRIPTION}}", "match": {
                    "type": "keyword", "pattern": "expected-failure", "mode": "or", "expected": True,
                }}
            ],
        },
    }
    mapping = [{
        "source_kbd_id": 1, "source_kbd_revision": 1, "source_signal_id": "task-description",
        "acquire_tool": "qkv_task", "category_scope": "category", "command_scope": "*",
        "priority": 1, "collector_id": "task-collector",
    }]
    evidence = [{
        "collector_id": "task-collector", "evidence_status": "available", "evidence_id": "evidence-1",
        "structured_data": {"data": [{"description": "different-failure"}]},
    }]

    evaluation = service._evaluate_signal(1, 1, "support", "category", signal, evidence, mapping)

    assert evaluation["state"] == "NOT_MATCHED"
    assert "后处理未通过" in evaluation["reason"]


def test_offline_qkv_assertion_cannot_hide_missing_declared_variable():
    signal = {
        "acquire": {"tool": "qkv_task", "args": {}},
        "orchestrate": {
            "produces": [{"name": "HOST", "path": "host"}, {"name": "VM_ID", "path": "vm"}],
            "output_processing": [{"mode": "assert", "input": "{{HOST}}", "match": {
                "type": "keyword", "pattern": "node-a", "mode": "or", "expected": True,
            }}],
        },
    }

    assert _evaluate_offline_producer(signal, {"data": [{"host": "node-a"}]}) is False


def test_offline_producer_values_render_consumer_args_with_evidence_provenance():
    """离线消费者审计应记录生产变量的实际值和对应 Evidence 来源。"""

    service = OfflineAnalysisService(None)
    producer = {
        "id": "task-host",
        "acquire": {"tool": "qkv_task", "args": {"keyword": "迁移"}},
        "orchestrate": {"produces": [{"name": "HOST", "path": "host"}]},
    }
    mapping = [{
        "source_kbd_id": 101, "source_kbd_revision": 1, "source_signal_id": "task-host",
        "acquire_tool": "qkv_task", "category_scope": "storage", "command_scope": "*",
        "priority": 1, "collector_id": "task-collector",
    }]
    result = service._evaluate_signal(
        101,
        1,
        "KBD-101",
        "storage",
        producer,
        [{
            "evidence_id": "evidence-task", "collector_id": "task-collector", "evidence_status": "available",
            "structured_data": {"data": [{"host": "node-a"}]},
        }],
        mapping,
    )

    assert result["state"] == "MATCHED"
    assert result["produced_values"] == {"host": "node-a"}
    assert result["matcher_snapshot"]["_produced_values"] == {"host": "node-a"}

    consumer = {
        "id": "host-log",
        "acquire": {"tool": "qfk_log", "args": {"resource_keyword": "迁移", "host": "{{HOST}}"}},
        "match": {"type": "keyword", "pattern": "done", "expected": True},
    }
    consumer_result = service._evaluate_signal(
        101,
        1,
        "KBD-101",
        "storage",
        consumer,
        [],
        [{
            "source_kbd_id": 101, "source_kbd_revision": 1, "source_signal_id": "host-log",
            "acquire_tool": "qfk_log", "category_scope": "storage", "command_scope": "*",
            "priority": 1, "collector_id": "host-log-collector",
        }],
        variable_values={
            "host": ProducedVariable(
                value="node-a",
                evidence_refs=("evidence-task",),
                source_signal_refs=("101/1/task-host",),
            )
        },
    )

    snapshot = consumer_result["matcher_snapshot"]
    assert snapshot["_resolved_acquire_args"]["host"] == "node-a"
    assert snapshot["_variable_provenance"]["HOST"] == {
        "evidence_refs": ["evidence-task"],
        "source_signal_refs": ["101/1/task-host"],
    }


def test_offline_producer_values_include_qkv_derived_variables():
    """后处理派生变量与原始 produces 必须共同进入共享变量池。"""

    outcome, values = _extract_offline_producer_values(
        {
            "acquire": {"tool": "qkv_task", "args": {}},
            "orchestrate": {
                "produces": [{"name": "DESCRIPTION", "path": "description"}],
                "output_processing": [
                    {
                        "mode": "derive",
                        "input": "{{DESCRIPTION}}",
                        "name": "VM_NAME",
                        "type": "string",
                        "extract": {"type": "feature", "feature": "vm_name", "cardinality": "exactly_one"},
                    }
                ],
            },
        },
        {"data": [{"description": "虚拟机名称：vm-001"}]},
    )

    assert outcome is True
    assert values == {"description": "虚拟机名称：vm-001", "vm_name": "vm-001"}


@pytest.mark.asyncio
async def test_offline_effect_replay_uses_only_completed_record_from_same_diagnosis_run():
    """离线回放复用已落库效果判定，不补跑观测也不猜测动作效果。"""

    class Result:
        def mappings(self):
            return self

        def all(self):
            return [{
                "verification_id": "11111111-1111-1111-1111-111111111111",
                "source_kbd_id": "101",
                "signal_id": "effect-1",
                "verdict": "not_achieved",
                "error_code": None,
                "completed_at": "2026-09-10T00:00:00Z",
                "trace_id": "trace-effect",
            }]

    class Session:
        async def execute(self, *_args):
            assert _args[1]["diagnosis_run_id"] == "run-1"
            return Result()

    evaluations = await OfflineAnalysisService(Session())._replay_effect_verifications(
        diagnosis_run_id="run-1",
        kbd_rows=[
            {
                "id": 101,
                "support_id": "KBD-101",
                "signals_json": {"signals": [{"id": "effect-1", "acquire": {"tool": "qkv_effect"}}]},
            },
            {
                "id": 102,
                "support_id": "KBD-102",
                "signals_json": {"signals": [{"id": "effect-missing", "acquire": {"tool": "qkv_effect"}}]},
            },
        ],
    )

    assert evaluations[0]["state"] == "MATCHED"
    assert evaluations[0]["matcher_snapshot"]["verdict"] == "not_achieved"
    assert evaluations[1]["state"] == "UNKNOWN"
