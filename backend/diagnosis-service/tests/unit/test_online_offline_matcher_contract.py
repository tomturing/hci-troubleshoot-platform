"""在线 Shared Matcher 与离线证据适配的 Golden Contract（黄金契约）回归。"""

from unittest.mock import MagicMock

import pytest
from app.services.offline_analysis_service import OfflineAnalysisService, _evaluate_matcher, _flatten_text
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
