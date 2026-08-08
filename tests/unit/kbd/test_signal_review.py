"""KBD 全量 Signal Review 与数据库只读加载回归。"""

from __future__ import annotations

import io
import json

import pytest
from kbd.run import build_parser
from kbd.signal_review import load_rows, load_rows_from_db, review_rows
from shared.resolution.review import SignalReviewFeature, review_signal_document


def _text_extract() -> dict:
    return {
        "type": "text",
        "rows": {"mode": "all"},
        "cardinality": "all",
        "source": "stdout",
    }


def _valid_signal_document() -> dict:
    return {
        "schema_version": 2,
        "signals": [
            {
                "id": "task",
                "acquire": {"tool": "qkv_task", "args": {"keyword": "启动虚拟机"}},
                "match": None,
                "orchestrate": {
                    "phase": "diagnostic",
                    "requires": [],
                    "produces": [{"name": "TASK", "path": "task"}],
                },
            },
            {
                "id": "system",
                "acquire": {"tool": "qfk_system", "args": {"command": "ps"}},
                "match": {
                    "type": "keyword",
                    "pattern": "kube",
                    "expected": True,
                    "extract": _text_extract(),
                },
                "orchestrate": {"phase": "diagnostic", "requires": [], "produces": []},
            },
        ],
    }


def _signal(signal_id: str, tool: str, args: dict, matcher: dict | None = None) -> dict:
    return {
        "id": signal_id,
        "acquire": {"tool": tool, "args": args},
        "match": matcher,
        "orchestrate": {"produces": []},
    }


def _review_fixture_rows() -> list[dict]:
    return [
        {
            "support_id": "ok",
            "signals_json": {
                "signals": [
                    _signal(
                        "log_ok",
                        "qfk_log",
                        {"file": "kernel.log"},
                        {"type": "keyword", "pattern": "I/O error"},
                    )
                ]
            },
        },
        {
            "support_id": "blocked",
            "signals_json": {
                "signals": [
                    _signal("missing_file", "qfk_log", {}, {"type": "exists"}),
                    _signal("dialog", "qkv_dialog", {"keyword": "失败"}),
                    _signal(
                        "bmc",
                        "qfk_log",
                        {"file": "BMC_Event_Log"},
                        {"type": "keyword", "pattern": "restart"},
                    ),
                ]
            },
        },
        {
            "support_id": "review",
            "signals_json": {
                "signals": [
                    _signal(
                        "log_ok",
                        "qfk_log",
                        {"file": "messages"},
                        {"type": "regex", "pattern": "error.*timeout"},
                    )
                ],
                "rejected_candidates": [
                    {
                        "reason": "外部日志源",
                        "candidate": _signal(
                            "external",
                            "qfk_log",
                            {"file": "NBU作业日志"},
                            {"type": "keyword", "pattern": "failed"},
                        ),
                    }
                ],
            },
        },
    ]


def test_review_reports_all_signal_tools_and_rejected_candidate():
    report = review_rows(_review_fixture_rows())

    assert report["case_count"] == 3
    assert report["review_engine"] == "shared_resolution_runtime"
    assert report["signal_type_counts"] == {"qfk_log": 4, "qkv_dialog": 1}
    assert report["case_status_counts"] == {"BLOCKED_SIGNAL_REVIEW": 3}
    assert report["issue_counts"]["REJECTED_SIGNAL_CANDIDATE"] == 1


def test_shared_review_covers_qkv_and_qfk_with_one_runtime_result():
    result = review_signal_document(
        _valid_signal_document(),
        feature=SignalReviewFeature.PIPELINE,
    )

    assert result.status.value == "passed"
    assert result.signal_count == 2
    assert result.runtime_status_counts == {"verified": 2}
    assert {item.tool for item in result.signals} == {"qkv_task", "qfk_system"}


def test_agent_execution_requires_live_verified_resolution_for_log_signal():
    document = {
        "schema_version": 2,
        "signals": [
            {
                "id": "log",
                "acquire": {"tool": "qfk_log", "args": {"file": "messages"}},
                "match": {"type": "keyword", "pattern": "error", "expected": True, "extract": _text_extract()},
                "orchestrate": {"phase": "diagnostic", "requires": [], "produces": []},
            }
        ],
    }
    result = review_signal_document(
        document,
        feature=SignalReviewFeature.AGENT_EXECUTION,
        require_verified=True,
    )

    assert result.status.value == "blocked"
    assert "SIGNAL_RUNTIME_NOT_VERIFIED" in {issue.code for issue in result.issues}


@pytest.mark.parametrize("old_command", ["audit", "audit-signals", "audit-log-signals"])
def test_removed_review_commands_are_not_accepted(old_command: str):
    with pytest.raises(SystemExit):
        build_parser().parse_args([old_command, "--all"])


def test_load_rows_requires_json_object_array():
    rows = _review_fixture_rows()
    assert load_rows(io.StringIO(json.dumps(rows, ensure_ascii=False))) == rows

    with pytest.raises(ValueError, match="JSON 数组"):
        load_rows(io.StringIO('{"support_id": "1"}'))
    with pytest.raises(ValueError, match="非法下标: 1"):
        load_rows(io.StringIO('[{"support_id": "1"}, null]'))


class _FakePool:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []

    async def fetch(self, sql: str, *args):
        self.calls.append((sql, args))
        return [{"support_id": "37150", "signals_json": {"signals": []}}]


@pytest.mark.asyncio
async def test_load_rows_from_db_is_select_only_and_supports_selected_ids():
    pool = _FakePool()

    rows = await load_rows_from_db(pool, ["37150"])

    assert rows == [{"support_id": "37150", "signals_json": {"signals": []}}]
    sql, args = pool.calls[0]
    assert sql.lstrip().startswith("SELECT support_id, signals_json")
    assert "UPDATE" not in sql.upper()
    assert args == (["37150"],)


@pytest.mark.asyncio
async def test_load_rows_from_db_empty_selection_does_not_query():
    pool = _FakePool()

    assert await load_rows_from_db(pool, []) == []
    assert pool.calls == []
