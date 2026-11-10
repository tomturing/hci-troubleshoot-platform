"""日志上传管理端只读端点的契约测试。

锁定四条边界：
1. 筛选参数全部走 bind params，枚举白名单，不拼 SQL；
2. 客户名来自 case.customer_id → customer.name 的 JOIN，未关联时为空，client_id 独立返回；
3. 列表接口完全不查 5888（真实进度走单独端点，避免 N×轮询）；
4. 「查不到 ≠ 失败」：404 / found=false / 会话代际不符 / 网络异常一律降级，
   只有 done + target_path 才是强证据。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from app.routes import log_collect_admin_tasks as routes
from fastapi import HTTPException


@pytest.fixture(autouse=True)
def _clear_cache():
    routes._progress_cache.clear()
    yield
    routes._progress_cache.clear()


def _now() -> datetime:
    return datetime.now(UTC)


def test_filters_are_parameterized():
    now = _now()
    where, params = routes._build_filters(
        since=now - timedelta(days=7),
        until=now,
        case_id="Q123",
        node_ip="172.28",
        upload_mode="acli_direct",
        status="uploading",
        delivery_status="delivered",
    )
    assert ":case_id" in where and params["case_id"] == "%Q123%"
    assert ":node_ip" in where and params["node_ip"] == "%172.28%"
    assert "CAST(t.origin_node_ip AS text)" in where
    assert params["upload_mode"] == "acli_direct"
    assert "Q123" not in where


@pytest.mark.parametrize(
    "field,value",
    [("status", "drop_table"), ("delivery_status", "hacked"), ("upload_mode", "sneaky")],
)
def test_filters_reject_unknown_enums(field, value):
    now = _now()
    kwargs = {
        "since": now - timedelta(days=7),
        "until": now,
        "case_id": None,
        "node_ip": None,
        "upload_mode": None,
        "status": None,
        "delivery_status": None,
    }
    kwargs[field] = value
    with pytest.raises(HTTPException) as exc:
        routes._build_filters(**kwargs)
    assert exc.value.status_code == 400


def test_summary_maps_p1_and_p2_columns():
    row = {
        "total": 10,
        "transferring": 3,
        "delivered": 5,
        "unverified": 1,
        "failed": 1,
        "p1_total": 6,
        "p1_transferring": 2,
        "p1_delivered": 3,
        "p1_unverified": 1,
        "p1_failed": 0,
        "p2_total": 4,
        "p2_transferring": 1,
        "p2_delivered": 2,
        "p2_unverified": 0,
        "p2_failed": 1,
    }
    summary = routes._summary_of(row)
    assert (summary.total, summary.delivered) == (10, 5)
    assert (summary.p1.total, summary.p1.delivered) == (6, 3)
    assert (summary.p2.total, summary.p2.failed) == (4, 1)


@pytest.mark.asyncio
async def test_list_returns_both_customer_name_and_client_id_without_querying_5888(monkeypatch):
    task_id = uuid4()
    fetched: list[str] = []

    async def fake_fetch(sql, params):
        fetched.append(sql)
        if "ORDER BY t.created_at DESC" in sql:
            return [
                {
                    "task_id": task_id,
                    "case_id": "Q2026111000243",
                    "client_id": "client-abc",
                    "customer_name": "示例客户",
                    "origin_node_ip": "172.28.25.1",
                    "upload_mode": "acli_direct",
                    "status": "completed",
                    "progress": 100,
                    "archive_size": 885094930,
                    "delivery_status": "delivered",
                    "relay_status": "none",
                    "relay_upload_id": None,
                    "integrity_state": "verified",
                    "archive_check": {"verified": True, "checked_at": "2026-11-10T11:02:02+00:00"},
                    "error_code": None,
                    "error_message": None,
                    "trace_id": "trace-1",
                    "created_at": _now() - timedelta(minutes=10),
                    "finished_at": _now(),
                    "delivery_finished_at": _now(),
                }
            ]
        if "count(*) FILTER" in sql:
            return [
                {
                    "total": 1,
                    "transferring": 0,
                    "delivered": 1,
                    "unverified": 0,
                    "failed": 0,
                    "p1_total": 1,
                    "p1_transferring": 0,
                    "p1_delivered": 1,
                    "p1_unverified": 0,
                    "p1_failed": 0,
                    "p2_total": 0,
                    "p2_transferring": 0,
                    "p2_delivered": 0,
                    "p2_unverified": 0,
                    "p2_failed": 0,
                }
            ]
        return [{"total": 1}]

    def must_not_be_called(*_args, **_kwargs):
        raise AssertionError("列表接口不允许查询 5888")

    monkeypatch.setattr(routes, "_fetch", fake_fetch)
    monkeypatch.setattr(routes, "SangforRelayClient", must_not_be_called)

    response = await routes.list_admin_tasks(
        page=1,
        page_size=20,
        days=7,
        case_id=None,
        node_ip=None,
        upload_mode=None,
        status=None,
        delivery_status=None,
    )
    assert len(fetched) == 3
    assert response.total == 1
    assert response.items[0].customer_name == "示例客户"
    assert response.items[0].client_id == "client-abc"
    assert response.summary.p1.delivered == 1


def _task_row(mode: str = "acli_direct") -> dict:
    return {
        "task_id": uuid4(),
        "case_id": "Q2026111000243",
        "upload_mode": mode,
        "relay_upload_id": None if mode == "acli_direct" else "88e83f26-f7d8-4cac-b62c-a297bb6da167",
        "created_at": _now() - timedelta(minutes=10),
        "archive_size": 885094930,
        "delivery_status": "delivered",
    }


def _patch_task(monkeypatch, row: dict) -> None:
    async def fake_fetch(_sql, _params):
        return [row]

    monkeypatch.setattr(routes, "_fetch", fake_fetch)


class _FakeClient:
    def __init__(self, *, progress=("ok", None), status=None):
        self.progress = progress
        self.status = status
        self.calls: list[str] = []

    def __call__(self, *_args, **_kwargs):
        return self

    async def upload_progress(self, _client, _filename):
        self.calls.append("upload_progress")
        return self.progress

    async def upload_status(self, _client, _upload_id):
        self.calls.append("upload_status")
        return self.status


@pytest.mark.asyncio
async def test_p1_404_degrades_to_unsupported(monkeypatch):
    """旧版 5888 没有该端点：平台先上也能正常返回，不报失败。"""
    row = _task_row()
    _patch_task(monkeypatch, row)
    fake = _FakeClient(progress=("unsupported", None))
    monkeypatch.setattr(routes, "SangforRelayClient", fake)

    result = await routes.destination_progress(row["task_id"])
    assert (result.path, result.state) == ("P1", "unsupported")
    assert "不支持进度查询" in (result.reason or "")
    assert fake.calls == ["upload_progress"]


@pytest.mark.asyncio
async def test_p1_not_found_is_not_a_failure(monkeypatch):
    row = _task_row()
    _patch_task(monkeypatch, row)
    fake = _FakeClient(progress=("ok", {"found": False}))
    monkeypatch.setattr(routes, "SangforRelayClient", fake)

    result = await routes.destination_progress(row["task_id"])
    assert result.state == "no_record"
    assert result.reason and "还没有这次上传的记录" in result.reason


@pytest.mark.asyncio
async def test_stale_session_is_not_mistaken_for_this_round(monkeypatch):
    row = _task_row()
    _patch_task(monkeypatch, row)
    stale = {
        "found": True,
        "status": "done",
        "target_path": "/sf/log/Q2026111000243/Q2026111000243.zip",
        "start_time": (row["created_at"] - timedelta(days=3)).isoformat(),
    }
    fake = _FakeClient(progress=("ok", stale))
    monkeypatch.setattr(routes, "SangforRelayClient", fake)

    result = await routes.destination_progress(row["task_id"])
    assert result.state == "no_record"
    assert result.reason and "代际不匹配" in result.reason


@pytest.mark.asyncio
async def test_only_done_with_target_path_is_strong_evidence(monkeypatch):
    row = _task_row()
    _patch_task(monkeypatch, row)
    data = {
        "found": True,
        "status": "done",
        "target_path": "/sf/log/Q2026111000243/Q2026111000243.zip",
        "uploaded_size": 885094930,
        "file_size": 885094930,
        "progress": 100,
        "digest": {"sha256": "c" * 64},
        "start_time": row["created_at"].isoformat(),
    }
    fake = _FakeClient(progress=("ok", data))
    monkeypatch.setattr(routes, "SangforRelayClient", fake)

    result = await routes.destination_progress(row["task_id"])
    assert result.state == "done"
    assert result.progress == 100
    assert result.sha256 == "c" * 64
    assert result.target_path.endswith(".zip")


@pytest.mark.asyncio
async def test_p2_uses_relay_upload_id(monkeypatch):
    row = _task_row("bridge_relay")
    _patch_task(monkeypatch, row)
    fake = _FakeClient(status={"status": "uploading", "uploaded_size": 100, "file_size": 200, "progress": 50})
    monkeypatch.setattr(routes, "SangforRelayClient", fake)

    result = await routes.destination_progress(row["task_id"])
    assert (result.path, result.state) == ("P2", "uploading")
    assert result.progress == 50
    assert fake.calls == ["upload_status"]


@pytest.mark.asyncio
async def test_progress_cache_prevents_repeated_5888_calls(monkeypatch):
    row = _task_row()
    _patch_task(monkeypatch, row)
    fake = _FakeClient(progress=("unsupported", None))
    monkeypatch.setattr(routes, "SangforRelayClient", fake)

    await routes.destination_progress(row["task_id"])
    await routes.destination_progress(row["task_id"])
    assert fake.calls == ["upload_progress"]
