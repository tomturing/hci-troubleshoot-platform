"""network-probe 证据写入 SQL 绑定参数回归守护。

现场事故（2026-11-26 staging）：``put_network_probe`` 的 UPDATE 语句末尾误粘了
artifact-manifest 的条件 ``OR :quality = 'ok'``，但参数字典只传了 ``task_id``，
导致每一次 ``POST /api/direct/log-collect/tasks/{id}/network-probe`` 全部触发
``sqlalchemy.exc.InvalidRequestError: A value is required for bind parameter 'quality'``
返回 HTTP 500，造成两日内所有任务的网络探测表几乎为空。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from app.routes import log_collect_evidence as ev
from app.schemas.log_collect_evidence import NetworkProbeIn
from sqlalchemy.dialects import postgresql


class _FakeSession:
    """用真实 PostgreSQL dialect 编译并校验每个 SQL 的命名绑定参数。"""

    def __init__(self, task_row: dict):
        self._task_row = task_row
        self.executed: list[tuple[str, dict]] = []
        self.committed = False

    async def execute(self, stmt, params=None):
        params = params or {}
        compiled = stmt.compile(dialect=postgresql.dialect())
        # 复现 SQLAlchemy 的绑定校验：只要 SQL 声明了某个命名参数而 params 未提供，立即抛错。
        missing = set(compiled.params.keys()) - set(params.keys())
        assert not missing, f"SQL 存在未提供的绑定参数: {missing}\nSQL={compiled}"
        self.executed.append((str(compiled), dict(params)))
        result = AsyncMock()
        result.mappings = lambda: SimpleNamespace(all=lambda: [self._task_row])
        return result

    async def commit(self):
        self.committed = True


@pytest.mark.asyncio
async def test_put_network_probe_sql_has_no_unbound_quality_param(monkeypatch):
    task_id = uuid4()
    session = _FakeSession(
        {
            "task_id": task_id,
            "case_id": "Q2026112601001",
            "upload_mode": "acli_direct",
            "trace_id": "lc-Q2026112601001-test",
        }
    )
    service = SimpleNamespace(repository=SimpleNamespace(session=session))
    monkeypatch.setattr(ev.tickets, "verify", lambda tid, tok: True)
    monkeypatch.setattr(ev.tickets, "issue", lambda pid, probe=False: "probe-ticket")

    payload = NetworkProbeIn(
        target="log_server",
        reachable=True,
        sample_count=3,
        loss_rate=0.0,
        rtt_min_ms=10,
        rtt_avg_ms=12,
        rtt_max_ms=15,
        jitter_ms=2,
        http_code=200,
        samples=[],
        bridge_version="v2.38.0-4-g0a1f20b4",
    )

    ack = await ev.put_network_probe(
        task_id=task_id,
        payload=payload,
        x_evidence_ticket="valid-ticket",
        x_bridge_version="v2.38.0-4-g0a1f20b4",
        service=service,
    )

    assert ack.task_id == task_id
    assert session.committed is True
    # 至少包含 task 查询、bridge_version 记录、probe 插入、detail.network_probes 回刷。
    assert len(session.executed) >= 3
    assert all("artifact_quality" not in sql for sql, _ in session.executed)
