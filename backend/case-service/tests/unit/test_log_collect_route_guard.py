"""日志收集路由的鉴权与 409 摘要测试（方案 A）。

跨客户端复用同一工单号后：
- 任务按 ``created_by`` 鉴权，非创建者访问统一 404；
- 409 的 ``active_task`` 摘要只含状态/进度/是否本人，不得含采集目标与制品路径。
"""

from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from app.routes.log_collect import _active_task_summary, _owned_task, create_task
from app.schemas.log_collect import LogCollectTaskCreate
from app.services.log_collect_service import TaskConflictError
from fastapi import HTTPException


def make_task(created_by: str = "client-a") -> SimpleNamespace:
    return SimpleNamespace(
        task_id=uuid4(),
        case_id="Q2026101401684",
        status="collecting",
        progress=42,
        created_by=created_by,
        created_at=datetime.now(UTC),
        # 以下字段不得出现在跨客户端摘要里
        origin_node_ip="10.0.0.1",
        ips=["10.0.0.2"],
        archive_path="/sf/secret/x.zip",
    )


def make_payload() -> LogCollectTaskCreate:
    return LogCollectTaskCreate(
        case_id="Q2026101401684",
        origin_node_ip="10.0.0.1",
        modules=["vm_suspend"],
        date_from=date(2026, 10, 21),
        date_to=date(2026, 10, 22),
    )


def test_active_task_summary_is_redacted():
    """摘要只含状态/进度/是否本人，不含节点与制品信息。"""
    summary = _active_task_summary(make_task(created_by="client-b"), "client-a")

    assert summary["status"] == "collecting"
    assert summary["progress"] == 42
    assert summary["created_by_me"] is False
    assert "origin_node_ip" not in summary
    assert "ips" not in summary
    assert "archive_path" not in summary


@pytest.mark.asyncio
async def test_owned_task_rejects_other_creator():
    """非任务创建者访问统一 404（不泄露任务存在性）。"""
    task = make_task(created_by="client-b")
    service = SimpleNamespace(get_task=AsyncMock(return_value=task))

    with pytest.raises(HTTPException) as exc:
        await _owned_task(task.task_id, "client-a", service)

    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_owned_task_allows_creator():
    """创建者可以访问自己在该工单下的任务。"""
    task = make_task(created_by="client-a")
    service = SimpleNamespace(get_task=AsyncMock(return_value=task))

    assert await _owned_task(task.task_id, "client-a", service) is task


@pytest.mark.asyncio
async def test_create_task_conflict_returns_redacted_active_task():
    """409 必须携带脱敏摘要，前端据此展示"查看任务状态"。"""
    active = make_task(created_by="client-a")
    service = SimpleNamespace(
        repository=SimpleNamespace(session=AsyncMock()),
        create_task=AsyncMock(side_effect=TaskConflictError("该工单已有进行中的日志收集任务", active_task=active)),
    )

    with patch("app.routes.log_collect.verify_case_exists", new=AsyncMock()):
        with pytest.raises(HTTPException) as exc:
            await create_task(make_payload(), client_id="client-a", service=service)

    assert exc.value.status_code == 409
    active_summary = exc.value.detail["active_task"]
    assert active_summary["task_id"] == str(active.task_id)
    assert active_summary["created_by_me"] is True
    assert "origin_node_ip" not in active_summary
    assert "ips" not in active_summary
