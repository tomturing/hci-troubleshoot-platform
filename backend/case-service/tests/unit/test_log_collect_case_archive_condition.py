"""工单级归档事实查询的回归测试。

该查询只提供观察条件，不能污染任务结论；口径必须严格于普通交付证据，
且不能按 created_by 过滤，否则跨客户端复用同一工单号时会漏报。
"""

from __future__ import annotations

import asyncio

from app.routes import log_collect as route


class _Result:
    def __init__(self, found: bool) -> None:
        self._found = found

    def first(self):
        return object() if self._found else None


class _Session:
    def __init__(self, found: bool) -> None:
        self.found = found
        self.sql = ""
        self.params: dict[str, object] = {}

    async def execute(self, statement, params):
        self.sql = str(statement)
        self.params = dict(params)
        return _Result(self.found)


def test_case_archive_query_uses_strict_evidence_without_owner_filter() -> None:
    session = _Session(found=True)
    assert asyncio.run(route._case_archive_verified("Q2026112600025", session)) is True
    assert session.params == {"case_id": "Q2026112600025"}
    assert "created_by" not in session.sql
    assert "archive_confirmation" in session.sql
    assert "p1_archive_check" in session.sql
    assert "delivery_evidence" in session.sql


def test_case_archive_query_returns_false_without_evidence() -> None:
    assert asyncio.run(route._case_archive_verified("Q2026111300153", _Session(found=False))) is False
