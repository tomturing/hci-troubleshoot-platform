"""节点清理路径的白名单：宁可不给命令，也不能给错路径。

回归背景（2026-11-06，Q2026110600940）：P2 结果态曾用平台对象 key
`Q2026110600940/<task-id>.zip` 拼 `rm -f`，这条命令在 HCI 节点上既删不掉包，
又可能误删同名文件。用户不懂平台内部字段，因此路径必须在两端都过白名单。
"""

from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from app.schemas.log_collect import LogCollectTaskResponse
from app.services.log_collect_node_path import (
    NODE_ARCHIVE_DIR,
    expected_node_archive_path,
    trusted_node_archive_path,
)

CASE = "Q2026110600940"
TRUSTED = f"{NODE_ARCHIVE_DIR}/{CASE}.zip"


def test_expected_path_is_absolute_and_tied_to_case():
    assert expected_node_archive_path(CASE) == TRUSTED
    assert expected_node_archive_path(CASE).startswith("/sf/")


@pytest.mark.parametrize("bad", ["", "Q123", "2026110600940", "Q20261106009401", None, 123])
def test_expected_path_rejects_invalid_case_id(bad):
    assert expected_node_archive_path(bad) is None


def test_trusted_accepts_only_the_exact_path():
    assert trusted_node_archive_path(CASE, TRUSTED) == TRUSTED


@pytest.mark.parametrize(
    "candidate",
    [
        None,
        "",
        # 平台对象 key：本次事故的元凶，相对路径
        f"{CASE}/dea7ccec-e402-43fd-997b-c6f59859c509.zip",
        # 路径穿越 / 任意绝对路径
        f"{NODE_ARCHIVE_DIR}/../../etc/passwd",
        "/etc/passwd",
        "/sf/data/local/acli/plugins/tasks",
        # 别的工单的包
        f"{NODE_ARCHIVE_DIR}/Q2026110600341.zip",
        # 后缀、空格、大小写差异一律不认
        f"{TRUSTED}.bak",
        f" {TRUSTED}",
        TRUSTED.upper(),
    ],
)
def test_trusted_rejects_everything_else(candidate):
    # 不合格一律不返回，绝不能降级成 candidate 本身展示给用户
    assert trusted_node_archive_path(CASE, candidate) is None


def test_trusted_rejects_invalid_case_id_even_with_looking_path():
    assert trusted_node_archive_path("Q123", f"{NODE_ARCHIVE_DIR}/Q123.zip") is None


async def test_attach_only_when_bridge_log_has_evidence():
    """服务端只在桥日志里核到节点侧报告时才下发路径。"""
    from app.routes import log_collect as routes

    session = SimpleNamespace(execute=AsyncMock())
    session.execute.return_value = SimpleNamespace(first=lambda: (1,))
    response = SimpleNamespace(node_archive_path=None)

    await routes._attach_node_archive_path([response], session, CASE)

    assert response.node_archive_path == TRUSTED
    params = session.execute.call_args.args[1]
    assert params["case_id"] == CASE
    assert params["expected_path"] == TRUSTED


async def test_attach_stays_none_without_evidence():
    from app.routes import log_collect as routes

    session = SimpleNamespace(execute=AsyncMock())
    session.execute.return_value = SimpleNamespace(first=lambda: None)
    response = SimpleNamespace(node_archive_path=None)

    await routes._attach_node_archive_path([response], session, CASE)

    assert response.node_archive_path is None


async def test_attach_never_breaks_task_list_on_query_error():
    """只读旁证查询失败时不能让整个任务列表 500。"""
    from app.routes import log_collect as routes

    session = SimpleNamespace(execute=AsyncMock(side_effect=RuntimeError("证据表不可用")))
    response = SimpleNamespace(node_archive_path=None)

    await routes._attach_node_archive_path([response], session, CASE)

    assert response.node_archive_path is None


async def test_attach_skips_unparseable_case_id_without_query():
    from app.routes import log_collect as routes

    session = SimpleNamespace(execute=AsyncMock())
    response = SimpleNamespace(node_archive_path=None)

    await routes._attach_node_archive_path([response], session, "not-a-case")

    session.execute.assert_not_awaited()
    assert response.node_archive_path is None


# ── 路由接线：字段必须真的出现在 API 响应里 ────────────────────────────────


def _task_response(case_id: str = CASE) -> LogCollectTaskResponse:
    """构造最小合法任务响应，供路由函数直接调用。"""
    now = datetime.now(UTC)
    return LogCollectTaskResponse(
        task_id=uuid4(),
        case_id=case_id,
        origin_node_ip="10.0.0.1",
        modules=["vm_suspend"],
        date_from=date(2026, 11, 6),
        date_to=date(2026, 11, 6),
        ips=["all"],
        status="completed",
        progress=100,
        created_by="client",
        created_at=now,
        updated_at=now,
    )


async def test_list_tasks_returns_trusted_path_only_with_evidence(monkeypatch):
    """无证据时字段必须为空，且平台对象 key 绝不能冒充节点路径。"""
    from app.routes import log_collect as routes

    monkeypatch.setattr(routes, "verify_case_exists", AsyncMock())
    session = SimpleNamespace(execute=AsyncMock())
    session.execute.return_value = SimpleNamespace(first=lambda: (1,))
    item = _task_response()
    item.archive_path = f"{CASE}/task-1.zip"
    service = SimpleNamespace(
        repository=SimpleNamespace(session=session),
        list_tasks=AsyncMock(return_value=([item], 1)),
    )

    result = await routes.list_tasks(case_id=CASE, limit=20, offset=0, client_id="client", service=service)

    assert result["total"] == 1
    assert result["items"][0].node_archive_path == TRUSTED
    assert result["items"][0].archive_path == f"{CASE}/task-1.zip"


async def test_list_tasks_returns_none_without_node_evidence(monkeypatch):
    from app.routes import log_collect as routes

    monkeypatch.setattr(routes, "verify_case_exists", AsyncMock())
    session = SimpleNamespace(execute=AsyncMock())
    session.execute.return_value = SimpleNamespace(first=lambda: None)
    item = _task_response()
    item.archive_path = f"{CASE}/task-1.zip"
    service = SimpleNamespace(
        repository=SimpleNamespace(session=session),
        list_tasks=AsyncMock(return_value=([item], 1)),
    )

    result = await routes.list_tasks(case_id=CASE, limit=20, offset=0, client_id="client", service=service)

    assert result["items"][0].node_archive_path is None


async def test_get_task_attaches_trusted_path():
    from app.routes import log_collect as routes

    session = SimpleNamespace(execute=AsyncMock())
    session.execute.return_value = SimpleNamespace(first=lambda: (1,))
    item = _task_response()
    service = SimpleNamespace(
        repository=SimpleNamespace(session=session),
        get_task=AsyncMock(return_value=item),
    )

    response = await routes.get_task(task_id=item.task_id, client_id="client", service=service)

    assert response.node_archive_path == TRUSTED
