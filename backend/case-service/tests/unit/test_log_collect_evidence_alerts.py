"""回归：证据上报的旁路派生不得遮蔽 sqlalchemy.text。

现场（2026-11-24 Q2026091700436）：POST /process-logs 返回 500，
栈底是 ``TypeError: 'str' object is not callable`` —— `_sync_collect_alerts` 里
``text = gzip.decompress(content).decode(...)`` 遮蔽了模块级导入的 ``text``，
紧接着 ``text("SELECT detail ...")`` 就炸了。

后果不是"少一条告警"：桥收到 500 会认为证据没上报成功，证据清单因此缺失，
客户界面 S7 证据回传只能显示「未知」；而进程日志正文其实早已 commit 入库。
"""

from __future__ import annotations

import ast
import gzip
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from app.routes.log_collect_evidence import _sync_collect_alerts

# 与生产路径一致：只有 collection_log.log 会触发告警派生
PATH = "/sf/log/today/acli/plugins/log_collect/collection_log.log"
SOURCE = (
    Path(__file__).resolve().parents[2] / "app" / "routes" / "log_collect_evidence.py"
)
TRACE = "lc-Q2026091700436-1795507681995"


def _payload() -> MagicMock:
    payload = MagicMock()
    payload.host = "192.168.20.11"
    payload.collected_at = None
    return payload


@pytest.mark.asyncio
async def test_sync_collect_alerts_reaches_update_without_shadowing_text() -> None:
    """正常路径必须真的执行到 UPDATE，而不是在 SELECT 处抛 TypeError。"""
    session = AsyncMock()
    row = MagicMock()
    row.scalar.return_value = {"collect_alerts": None}
    session.execute.return_value = row
    service = MagicMock()
    service.repository.session = session

    content = gzip.compress(
        b"2026-11-23 15:12:23,893 - single_collect.py[line:265] - ERROR: "
        b"failed add /sf/etc/isv-guide/cert.png to tar, "
        b"error: [Errno 5] Input/output error\n"
    )
    await _sync_collect_alerts(
        service,
        task_id=uuid4(),
        path=PATH,
        payload=_payload(),
        content=content,
        trace_id=TRACE,
    )

    # 两次 execute：SELECT detail，然后 UPDATE detail
    assert session.execute.await_count == 2
    update_stmt = session.execute.await_args_list[1].args[0]
    # 第一参数必须是 SQLAlchemy 的 TextClause（被遮蔽时会退化成 str，这里就红）
    assert hasattr(update_stmt, "compile"), "SQL 必须是 TextClause，不能是 str"
    assert "UPDATE log_collect_task" in str(update_stmt)


@pytest.mark.asyncio
async def test_sync_collect_alerts_ignores_non_collection_log() -> None:
    """非采集插件日志直接返回：不查库，也不报错。"""
    session = AsyncMock()
    service = MagicMock()
    service.repository.session = session

    await _sync_collect_alerts(
        service,
        task_id=uuid4(),
        path="/sf/log/today/acli/acli.log",
        payload=_payload(),
        content=gzip.compress(b"anything"),
        trace_id=TRACE,
    )
    session.execute.assert_not_awaited()


def test_sync_does_not_assign_name_text() -> None:
    """静态防线：该函数内禁止再出现名为 text 的赋值（本次 bug 的最小复现条件）。"""
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == (
            "_sync_collect_alerts"
        ):
            assigned = {
                target.id
                for child in ast.walk(node)
                if isinstance(child, ast.Assign)
                for target in child.targets
                if isinstance(target, ast.Name)
            }
            assert "text" not in assigned, "禁止遮蔽 sqlalchemy.text"
            return
    raise AssertionError("_sync_collect_alerts 不存在")
