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
from app.domain.log_collect_display import derive
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
    [
        ("status", "creating"),
        ("status", "relaying"),
        ("delivery_status", "cancelled"),
        ("delivery_status", "queued"),
    ],
)
def test_filters_accept_every_value_offered_by_the_ui(field, value):
    """下拉里能看到的值必须全部合法，否则 400 会被旧表格伪装成「过滤失效」。"""
    kwargs = {
        "since": None,
        "until": None,
        "case_id": None,
        "node_ip": None,
        "upload_mode": None,
        "status": None,
        "delivery_status": None,
    }
    kwargs[field] = value
    where, params = routes._build_filters(**kwargs)
    assert ":synthetic_marker" in where
    assert params[field] == value


def test_filters_all_time_has_no_time_boundary_and_valid_sql():
    """days=0 的「全部」不能退化为更大天数，也不能拼出空 WHERE。"""
    where, params = routes._build_filters(
        since=None,
        until=None,
        case_id=None,
        node_ip=None,
        upload_mode=None,
        status=None,
        delivery_status=None,
        include_synthetic=True,
    )
    assert where == "TRUE"
    assert params == {}


def test_synthetic_tasks_excluded_unless_explicitly_included():
    """运营看板默认排除压测任务；NULL error_message 不能被正则误伤。"""
    kwargs = {
        "since": None,
        "until": None,
        "case_id": None,
        "node_ip": None,
        "upload_mode": None,
        "status": None,
        "delivery_status": None,
    }
    excluded, excluded_params = routes._build_filters(**kwargs)
    assert "COALESCE(t.error_message, '') !~* :synthetic_marker" in excluded
    assert excluded_params["synthetic_marker"]

    included, included_params = routes._build_filters(**kwargs, include_synthetic=True)
    assert "synthetic_marker" not in included
    assert "synthetic_marker" not in included_params


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


def _summary_row(mode: str, status: str, delivery: str, *, detail: dict | None = None) -> dict:
    return {
        "upload_mode": mode,
        "status": status,
        "progress": 40 if status == "collecting" else 100,
        "archive_size": 2 * 1024**3,
        "object_size": None,
        "delivery_status": delivery,
        "relay_status": "success" if mode == "bridge_relay" and delivery == "delivered" else "none",
        "archive_path": "/sf/log/Q2026111000243.zip",
        "detail": detail or {},
        "archive_check": None,
    }


def test_summary_maps_p1_and_p2_columns():
    """汇总不再吃 SQL 预聚合列：由同一批任务行推导，且 P1/P2 分组必然可加。"""
    rows = (
        [
            _summary_row("acli_direct", "completed", "delivered", detail={"p1_archive_check": {"verified": True}})
            for _ in range(3)
        ]
        + [_summary_row("acli_direct", "collecting", "none") for _ in range(2)]
        + [_summary_row("acli_direct", "failed", "none")]
        + [_summary_row("bridge_relay", "completed", "delivered") for _ in range(2)]
        + [_summary_row("bridge_relay", "collecting", "none")]
        + [_summary_row("bridge_relay", "failed", "none")]
    )

    summary = routes._summary_of(rows)

    assert (summary.total, summary.delivered) == (10, 5)
    assert (summary.p1.total, summary.p1.delivered) == (6, 3)
    assert (summary.p2.total, summary.p2.failed) == (4, 1)
    assert sum(summary.by_conclusion.values()) == summary.total == 10
    assert summary.transferring == summary.by_conclusion["pending"] == 3
    assert sum(summary.p1.by_conclusion.values()) == 6
    assert sum(summary.p2.by_conclusion.values()) == 4


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
    # 与旧 SQL 口径的差异：采集 completed 不代表流程结束，代传真的在跑就是传输中。
    # 真正要排除的僵尸是 completed + queued（交付根本没开始），下面那条仍然排除。
    ("completed", "relaying", True, "采集已完成+代传真的在跑=传输中"),
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
    """行为级验证：传输中 == conclusion 仍是 pending，不再检查 SQL 片段字符串。

    核心修复保留：采集终态（failed/completed/cancelled）+ delivery=queued 的
    僵尸任务不计入「传输中」；采集 completed 但代传真的在跑则计入。
    """
    summary = routes._summary_of([_summary_row("bridge_relay", status, delivery_status)])

    assert (summary.by_conclusion["pending"] == 1) is expected, desc
    assert sum(summary.by_conclusion.values()) == summary.total == 1


def test_summary_select_does_not_duplicate_status_rules():
    """汇总 SQL 只取原始事实，判定不能在 SQL 里再写一份。"""
    assert "count(*) FILTER" not in routes._SUMMARY_SELECT
    assert "jsonb_build_object" in routes._SUMMARY_SELECT
    assert routes._SUMMARY_SELECT.count("(") == routes._SUMMARY_SELECT.count(")")


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
        if "SELECT t.upload_mode" in sql:
            return [
                _summary_row("acli_direct", "completed", "delivered", detail={"p1_archive_check": {"verified": True}})
            ]
        if "log_collect_transfer_metric" in sql or "log_collect_network_probe" in sql:
            return []  # 该页没有证据样本：界面必须显示「—」而不是 0
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
    # 5 次查询：列表 / 总数 / 汇总瘦行 / 本页速率批量 / 本页探测批量。
    # 速率与探测各一次批量查询，不是按行 N+1。
    assert len(fetched) == 5
    assert sum("log_collect_transfer_metric" in sql for sql in fetched) == 1
    assert sum("log_collect_network_probe" in sql for sql in fetched) == 1
    assert response.total == 1
    assert response.items[0].customer_name == "示例客户"
    assert response.items[0].client_id == "client-abc"
    assert response.summary.p1.delivered == 1


@pytest.mark.asyncio
async def test_list_days_zero_drops_time_window(monkeypatch):
    """前端「全部」走 days=0：列表/总数/汇总三条 SQL 都不能带 created_at 边界。"""
    fetched: list[str] = []

    async def fake_fetch(sql, params):
        fetched.append(sql)
        return []

    monkeypatch.setattr(routes, "_fetch", fake_fetch)
    await routes.list_admin_tasks(
        page=1,
        page_size=20,
        days=0,
        case_id=None,
        node_ip=None,
        upload_mode=None,
        status=None,
        delivery_status=None,
        include_synthetic=True,
    )
    # 空列表时本页证据的批量查询直接跳过，只剩列表/总数/汇总三条。
    assert len(fetched) == 3
    assert all("created_at >=" not in sql and "created_at <=" not in sql for sql in fetched)
    assert all("WHERE TRUE" in sql for sql in fetched)


@pytest.mark.asyncio
async def test_list_default_excludes_synthetic_from_all_three_queries(monkeypatch):
    """列表/总数/汇总必须共用同一个压测过滤，否则统计与表格对不上。"""
    captured: list[dict] = []

    async def fake_fetch(sql, params):
        captured.append(params)
        return []

    monkeypatch.setattr(routes, "_fetch", fake_fetch)
    await routes.list_admin_tasks(
        page=1,
        page_size=20,
        days=7,
        case_id=None,
        node_ip=None,
        upload_mode=None,
        status=None,
        delivery_status=None,
    )
    assert len(captured) == 3
    assert all("synthetic_marker" in params for params in captured[:3])


@pytest.mark.asyncio
async def test_list_attaches_batched_transfer_and_network_evidence(monkeypatch):
    """列表里的速率/ETA/网络告警与详情同源，且只做批量查询。"""
    task_id = uuid4()

    async def fake_fetch(sql, params):
        if "ORDER BY t.created_at DESC" in sql:
            return [
                {
                    **_summary_row("bridge_relay", "completed", "relaying"),
                    "task_id": task_id,
                    "case_id": "Q2026111200624",
                    "client_id": None,
                    "customer_name": None,
                    "origin_node_ip": None,
                    "relay_upload_id": None,
                    "integrity_state": "unverified",
                    "error_code": None,
                    "error_message": None,
                    "trace_id": "tr-page",
                    "created_at": _now(),
                    "finished_at": None,
                    "delivery_finished_at": None,
                    "archive_check": None,
                }
            ]
        if "SELECT t.upload_mode" in sql:
            return [_summary_row("bridge_relay", "completed", "relaying")]
        if "log_collect_transfer_metric" in sql:
            return [
                {
                    "task_id": task_id,
                    "stage": "relaying",
                    "bytes_done": 1024,
                    "bytes_total": 2048,
                    "percent": 50,
                    "elapsed_ms": 1000,
                    "rate_bps": 1024,
                    "eta_sec": 1,
                    "stalled": True,
                    "source": "bridge",
                    "created_at": _now() - timedelta(minutes=3),
                }
            ]
        if "log_collect_network_probe" in sql:
            return [
                {
                    "task_id": task_id,
                    "target": "platform_data",
                    "reachable": True,
                    "grade": "poor",
                    "rtt_avg_ms": 320,
                    "jitter_ms": 90,
                    "loss_rate": 0.03,
                    "http_code": 200,
                    "up_bps": 1024,
                    "down_bps": 2048,
                    "advice": "弱网建议改时段",
                    "created_at": _now(),
                }
            ]
        return [{"total": 1}]

    monkeypatch.setattr(routes, "_fetch", fake_fetch)
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
    item = response.items[0]

    assert item.transfer is not None and item.transfer.rate_text == "1.00 KiB/s"
    assert item.network is not None and item.network.grade == "poor"
    conditions = [entry.key for entry in item.display.conditions]
    assert "stalled" in conditions and "network_degraded" in conditions
    assert item.display.conclusion == "pending"


def _task_row(mode: str = "acli_direct", status: str = "collecting") -> dict:
    return {
        "task_id": uuid4(),
        "case_id": "Q2026111000243",
        "upload_mode": mode,
        "relay_upload_id": None if mode == "acli_direct" else "88e83f26-f7d8-4cac-b62c-a297bb6da167",
        "created_at": _now() - timedelta(minutes=10),
        "archive_size": 885094930,
        "delivery_status": "delivered",
        "status": status,
    }


def _patch_task(monkeypatch, row: dict) -> None:
    async def fake_fetch(_sql, _params):
        return [row]

    monkeypatch.setattr(routes, "_fetch", fake_fetch)


class _FakeClient:
    def __init__(self, *, progress=("ok", None), status=None, archive=None):
        self.progress = progress
        self.status = status
        self.archive = archive
        self.calls: list[str] = []

    def __call__(self, *_args, **_kwargs):
        return self

    async def upload_progress(self, _client, _filename):
        self.calls.append("upload_progress")
        return self.progress

    async def upload_status(self, _client, _upload_id):
        self.calls.append("upload_status")
        return self.status

    async def archive_status(self, _client, _filename):
        self.calls.append("archive_status")
        return self.archive


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
async def test_failed_row_sees_archive_after_session_expired(monkeypatch):
    """终态会话会被 5888 清理；查不到会话不等于没归档。"""
    row = _task_row(status="failed")
    _patch_task(monkeypatch, row)
    archive = {
        "found": True,
        "status": "archived",
        "size": row["archive_size"],
        "mtime": int((_now() - timedelta(minutes=2)).timestamp()),
        "sha256": "d" * 64,
        "target_path": "/sf/log/Q2026111000243/Q2026111000243.zip",
        "finished_at": _now().isoformat(),
    }
    fake = _FakeClient(progress=("ok", {"found": False}), archive=archive)
    monkeypatch.setattr(routes, "SangforRelayClient", fake)

    result = await routes.destination_progress(row["task_id"])
    assert result.state == "done"
    assert result.target_path.endswith(".zip")
    assert result.reason == "来自 5888 归档记录"
    assert fake.calls == ["upload_progress", "archive_status"]


@pytest.mark.asyncio
async def test_stale_archive_is_not_claimed_as_this_round(monkeypatch):
    """对抗性守门：历史包不能冒充本轮送达。"""
    row = _task_row(status="failed")
    _patch_task(monkeypatch, row)
    archive = {
        "found": True,
        "size": row["archive_size"],
        "mtime": int((_now() - timedelta(days=2)).timestamp()),
        "target_path": "/sf/log/Q2026111000243/old.zip",
    }
    fake = _FakeClient(progress=("ok", {"found": False}), archive=archive)
    monkeypatch.setattr(routes, "SangforRelayClient", fake)

    result = await routes.destination_progress(row["task_id"])
    assert result.state == "no_record"
    assert "仅查到更早或不完整的归档" in (result.reason or "")


@pytest.mark.asyncio
async def test_active_row_does_not_query_archive(monkeypatch):
    """在途行仍只走会话查询，避免 N× 轮询打爆 5888。"""
    row = _task_row()
    _patch_task(monkeypatch, row)
    fake = _FakeClient(
        progress=("ok", {"found": False}),
        archive={"found": True, "size": row["archive_size"]},
    )
    monkeypatch.setattr(routes, "SangforRelayClient", fake)

    result = await routes.destination_progress(row["task_id"])
    assert result.state == "no_record"
    assert fake.calls == ["upload_progress"]


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


def test_list_detail_whitelist_keeps_stage_signals():
    """S1/S2 曾因白名单裁掉 probe 恒显 unknown：锁住派生输入，不改结论口径。"""
    for key in ("'probe'", "'create_ack'", "'evidence_enabled'", "'capabilities'"):
        assert key in routes._EVIDENCE_DETAIL
    assert routes._EVIDENCE_DETAIL in routes._SELECT

    row = {
        "upload_mode": "acli_direct",
        "status": "completed",
        "delivery_status": "delivered",
        "relay_status": "none",
        "archive_size": 2 * 1024**2,
        "archive_path": "/sf/log/Q.zip",
        "progress": 100,
        "error_code": None,
        "error_message": None,
        "trace_id": "trace-1",
        "detail": {
            "probe": {"hosts_count": 4, "acli_installed": True, "acli_version": "1.0.0"},
            "p1_archive_check": {"verified": True},
        },
    }
    without_probe = {**row, "detail": {"p1_archive_check": {"verified": True}}}
    with_probe = derive(row)
    without = derive(without_probe)
    stages = {item["key"]: item["status"] for item in with_probe["display"]["stages"]}
    assert stages["S1"] == "success"
    assert stages["S2"] == "success"
    # 探针只影响阶段状态，不改变交付结论与统计分桶。
    assert with_probe["display"]["conclusion"] == without["display"]["conclusion"]


@pytest.mark.asyncio
async def test_host_results_reads_bridge_payload_without_fabrication(monkeypatch):
    """逐台明细只回读桥端结构化事实：空 IP 过滤，不拿 hosts_count 造行。"""
    task_id = uuid4()

    async def fake_task_row(_task_id):
        return {"task_id": _task_id}

    async def fake_fetch(sql, params):
        assert "log_collect.host_results" in sql
        assert params == {"task_id": str(task_id)}
        return [
            {
                "hosts": [
                    {"ip": "10.0.0.1", "status": "成功", "percent": "100%"},
                    {"ip": "", "status": "脏数据"},
                ],
                "created_at": _now(),
            }
        ]

    monkeypatch.setattr(routes, "_task_row", fake_task_row)
    monkeypatch.setattr(routes, "_fetch", fake_fetch)
    response = await routes.host_results(task_id)
    assert [item.ip for item in response.items] == ["10.0.0.1"]
    assert response.items[0].status == "成功"
    assert response.items[0].percent == "100%"
