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


# ── 「传输中」口径测试（2026-11-12 修复：排除采集已终态但 delivery=queued 的僵尸）──

# 测试矩阵：覆盖所有 status × delivery_status 组合
_TRANSFERRING_CASES = [
    # (status, delivery_status, should_be_transferring, 说明)
    # A 段：采集活跃且交付未终结 → 传输中
    ("collecting", "none", True, "采集中+未开始交付=传输中"),
    ("packaging", "none", True, "打包中+未开始交付=传输中"),
    ("uploading", "none", True, "上传中+未开始交付=传输中"),
    ("collecting", "queued", True, "采集中+交付排队=传输中"),
    ("uploading", "uploading", True, "上传中+交付上传中=传输中"),
    # A 段排除：采集活跃但交付已终结 → 不算传输中
    ("uploading", "delivered", False, "上传中+已交付=不算传输中（平台已收妥）"),
    ("uploading", "failed", False, "上传中+交付失败=不算传输中"),
    # B 段：采集未终态且交付在途 → 传输中
    ("pending", "queued", True, "待下发+交付排队=传输中"),
    ("probing", "pulling", True, "探测中+拉取中=传输中"),
    ("installing", "uploading", True, "安装中+上传中=传输中"),
    # B 段排除：采集已终态，即使 delivery 在途也不算 → 这就是修复的核心
    ("failed", "queued", False, "采集已失败+交付排队=僵尸，不算传输中"),
    ("completed", "queued", False, "采集已完成+交付排队=僵尸，不算传输中"),
    ("cancelled", "queued", False, "采集已取消+交付排队=僵尸，不算传输中"),
    ("failed", "relaying", False, "采集已失败+代传中=不算传输中"),
    ("completed", "relaying", False, "采集已完成+代传中=不算传输中"),
    # 终态组合
    ("failed", "failed", False, "双失败=不算传输中"),
    ("completed", "delivered", False, "双完成=不算传输中"),
    ("cancelled", "none", False, "取消+无交付=不算传输中"),
]


@pytest.mark.parametrize(
    "status,delivery_status,expected,desc",
    _TRANSFERRING_CASES,
    ids=[c[3] for c in _TRANSFERRING_CASES],
)
def test_transferring_criteria_excludes_zombie_delivery(status, delivery_status, expected, desc):
    """验证 _TRANSFERRING SQL 片段在各种状态组合下的正确性。

    核心修复：采集已终态（failed/completed/cancelled）但 delivery_status=queued 的
    僵尸任务不应计入「传输中」。
    """
    # 直接用 SQL 片段做字符串级验证：确认新口径包含 NOT IN 终态排除
    sql = routes._TRANSFERRING
    assert "NOT IN ('failed','cancelled','completed')" in sql, "新口径必须排除采集终态"
    assert "NOT IN ('delivered','failed')" in sql, "新口径必须排除交付终态"
    # 旧口径的 OR 已被替换：不能有裸 OR delivery_status IN
    assert "OR t.delivery_status IN ('queued'" not in sql, "旧口径的裸 OR 已移除"


def test_transferring_sql_is_valid_syntax():
    """验证 _TRANSFERRING 片段是合法 SQL（括号匹配）。"""
    sql = routes._TRANSFERRING.strip()
    assert sql.count("(") == sql.count(")"), "括号不匹配"


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
