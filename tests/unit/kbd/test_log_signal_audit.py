"""KBD 日志 Proposal 领域审计与数据库只读加载回归。"""

from __future__ import annotations

import io
import json

import pytest
from kbd.log_signal_audit import audit_rows, load_rows, load_rows_from_db


def _signal(signal_id: str, tool: str, args: dict, matcher: dict | None = None) -> dict:
    return {
        "id": signal_id,
        "acquire": {"tool": tool, "args": args},
        "match": matcher,
        "orchestrate": {"produces": []},
    }


def _audit_fixture_rows() -> list[dict]:
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


def test_audit_distinguishes_pass_log_gap_and_rejected_candidate():
    report = audit_rows(_audit_fixture_rows())

    assert report["case_count"] == 3
    assert report["case_status_counts"] == {
        "BLOCKED_ACTIVE_SIGNAL": 1,
        "NEEDS_EXPERT_REVIEW": 1,
        "PASS_LOG_CONTRACT": 1,
    }
    assert report["case_status_ids"]["PASS_LOG_CONTRACT"] == ["ok"]
    assert report["issue_case_ids"]["CAPABILITY_GAP"] == ["blocked"]
    assert report["issue_counts"] == {
        "CAPABILITY_GAP": 1,
        "MISSING_FILE": 1,
        "REJECTED_LOG_CANDIDATE": 1,
    }


def test_load_rows_requires_json_object_array():
    rows = _audit_fixture_rows()
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
