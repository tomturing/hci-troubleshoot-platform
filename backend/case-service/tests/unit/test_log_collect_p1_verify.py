"""P1 归档核验的写入契约（2026-11-10 Q2026111000243）。

现场事实：工单 Q2026111000243 走 P1 直传，压缩包确实落到了日志服务器（/sf/log
下能读到），平台任务行却是 ``delivery_status=failed``，界面据此显示「交付失败」。

根因在写入契约，不在探测本身：``acli_direct`` 的 failed 只有 verify-archive 一个写入者，
而它把一次**瞬时**观测（采集刚结束时上传往往才刚开始，5888 也还没发布正式文件名）
直接当成终态失败写库，此后没有任何写入者会纠正它。

本文件锁定三条不变量：
1. 只有核验通过才写 delivered；
2. 否定结论不写交付终态、不写失败码，只留核验快照；
3. 已 delivered 的记录不会被一次否定观测降级。
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from app.routes import log_collect as lc_route
from app.routes.log_collect import (
    _judge_p1_archive,
    _judge_p1_probe,
    _p1_receipt_view,
    _p1_verify_patch,
    _probe_p1_archive,
    reconcile_p1_archives_once,
)


def _task(created_at: datetime | None = None, delivery_status: str = "none"):
    return SimpleNamespace(
        created_at=created_at or datetime.now(UTC) - timedelta(minutes=30),
        delivery_status=delivery_status,
    )


class TestJudge:
    """判据：存在 + 体积不小于记录值 + 不是历史遗留。"""

    def test_missing_archive_is_not_a_failure_conclusion(self):
        verified, reason, size, archived_at = _judge_p1_archive(_task(), {"found": False}, 100)
        assert (verified, reason, size, archived_at) == (False, "archive_missing", None, None)

    def test_truncated_archive_is_rejected(self):
        """截断只会让体积变小，所以"比记录值小"必须拦住。"""
        verified, reason, size, _ = _judge_p1_archive(
            _task(), {"found": True, "size": 80, "mtime": datetime.now(UTC).timestamp()}, 100
        )
        assert (verified, reason, size) == (False, "size_mismatch", 80)

    def test_archived_size_not_smaller_than_record_is_accepted(self):
        """期望值是平台观测到的峰值，可能停在 zip 收尾前一刻：更大的包不该被判失败。"""
        verified, reason, size, _ = _judge_p1_archive(
            _task(), {"found": True, "size": 120, "mtime": datetime.now(UTC).timestamp()}, 100
        )
        assert (verified, reason, size) == (True, "verified", 120)

    def test_unknown_expected_size_only_needs_non_empty_archive(self):
        verified, _, size, _ = _judge_p1_archive(
            _task(), {"found": True, "size": 120, "mtime": datetime.now(UTC).timestamp()}, None
        )
        assert (verified, size) == (True, 120)

    def test_archive_older_than_task_is_stale(self):
        created = datetime.now(UTC) - timedelta(minutes=5)
        stale = (created - timedelta(hours=6)).timestamp()
        verified, reason, _, _ = _judge_p1_archive(_task(created), {"found": True, "size": 100, "mtime": stale}, 100)
        assert (verified, reason) == (False, "stale_archive")

    def test_previous_archive_smaller_than_record_is_stale_first(self):
        """Q2026111700614：新包上传中、规范名仍挂旧包 → stale_archive，不是 size_mismatch。

        旧包体积必然小于本次记录值，但先判时间才能说清"新包还没发布"这个事实；
        展示层据此走有界等待（LC-HTP-001），而不是「大小不一致」红卡。
        """
        created = datetime.now(UTC) - timedelta(minutes=50)
        old_mtime = (created - timedelta(hours=3)).timestamp()
        verified, reason, size, _ = _judge_p1_archive(
            _task(created), {"found": True, "size": 314_820_302, "mtime": old_mtime}, 4_952_749_247
        )
        assert (verified, reason, size) == (False, "stale_archive", 314_820_302)

    def test_zero_size_archive_is_invalid(self):
        verified, reason, _, _ = _judge_p1_archive(_task(), {"found": True, "size": 0}, None)
        assert (verified, reason) == (False, "archive_size_invalid")

    def test_untrusted_expected_size_is_not_truncation(self):
        """Q2026112700769：期望值来自采集中观测（可能读到上一批次残留包）时，
        体积偏小只做待核对，不报 size_mismatch 红卡。"""
        verified, reason, size, _ = _judge_p1_archive(
            _task(),
            {"found": True, "size": 152_850_599, "mtime": datetime.now(UTC).timestamp()},
            4_478_420_847,
            expected_size_trustworthy=False,
        )
        assert (verified, reason, size) == (False, "archive_size_untrusted", 152_850_599)


class TestVerifyPatch:
    """只有强证据成立才允许写交付终态。"""

    def test_verified_writes_delivered(self):
        checked_at = datetime.now(UTC)
        patch = _p1_verify_patch(
            _task(),
            verified=True,
            check={"verified": True, "reason": "verified"},
            checked_at=checked_at,
        )
        assert patch.delivery_status == "delivered"
        assert patch.delivery_finished_at == checked_at
        assert patch.detail["delivery_evidence"] == "archive_verified"

    def test_negative_does_not_write_terminal_state(self):
        """否定观测只落快照：写 failed 会把"还在传"永久钉成"交付失败"。"""
        patch = _p1_verify_patch(
            _task(),
            verified=False,
            check={"verified": False, "reason": "archive_missing"},
            checked_at=datetime.now(UTC),
        )
        assert patch.delivery_status is None
        assert patch.delivery_error is None
        assert patch.delivery_finished_at is None
        assert patch.error_code is None
        assert patch.detail["p1_archive_check"]["reason"] == "archive_missing"
        assert patch.detail["delivery_evidence"] == "archive_unverified"

    def test_negative_never_downgrades_delivered(self):
        patch = _p1_verify_patch(
            _task(delivery_status="delivered"),
            verified=False,
            check={"verified": False, "reason": "archive_missing"},
            checked_at=datetime.now(UTC),
        )
        assert patch.delivery_status is None
        # 交付事实不可逆：连"未核验"的证据标注都不该覆盖掉已有的核验结论
        assert "delivery_evidence" not in patch.detail
        assert patch.detail["p1_archive_check"]["reason"] == "archive_missing"

    def test_patch_round_trip_drops_none_fields(self):
        """服务端按 exclude_none 应用 patch：没写的字段必须真的不出现在写入集里。"""
        patch = _p1_verify_patch(
            _task(),
            verified=False,
            check={"verified": False, "reason": "size_mismatch"},
            checked_at=datetime.now(UTC),
        )
        written = patch.model_dump(exclude_none=True)
        assert "delivery_status" not in written
        assert "delivery_finished_at" not in written


class TestReceiptView:
    """5888 上传会话回执 → 判定输入（2026-11-27 起核验优先用它）。"""

    @staticmethod
    def _receipt(**overrides):
        created = datetime.now(UTC) - timedelta(minutes=20)
        payload = {
            "found": True,
            "upload_id": "u-1",
            "status": "done",
            "file_size": 120,
            "uploaded_size": 120,
            "received_chunks": [0, 1, 2],
            "chunk_total": 3,
            "start_time": (created + timedelta(minutes=8)).isoformat(),
            "target_path": "/sf/log/Q1/Q1.zip",
            "digest": {"sha256": "a" * 64},
        }
        payload.update(overrides)
        return payload

    def test_done_session_becomes_archive_fact(self):
        receipt = self._receipt()
        view = _p1_receipt_view(receipt)
        assert view is not None
        assert view["state"] == "done"
        assert view["archive"]["size"] == 120
        assert view["meta"]["upload_id"] == "u-1"
        # 代际用会话起始时间，不用文件 mtime：后者在上传切包时会变。
        assert view["archive"]["mtime"] == datetime.fromisoformat(receipt["start_time"]).timestamp()

    def test_archive_fallback_without_upload_id_is_not_a_receipt(self):
        """会话过期后 5888 用归档件兜底：file_size==uploaded_size 是自洽结果，不是收齐证明。"""
        assert _p1_receipt_view(self._receipt(upload_id=None)) is None
        assert _p1_receipt_view({"found": False}) is None

    def test_uploading_session_is_pending(self):
        assert _p1_receipt_view(self._receipt(status="uploading"))["state"] == "pending"
        assert _p1_receipt_view(self._receipt(status="failed"))["state"] == "pending"

    def test_done_but_chunks_incomplete_is_invalid(self):
        view = _p1_receipt_view(self._receipt(received_chunks=[0, 1]))
        assert view["state"] == "invalid"

    def test_done_but_size_inconsistent_is_invalid(self):
        assert _p1_receipt_view(self._receipt(uploaded_size=119))["state"] == "invalid"

    def test_missing_start_time_is_unusable(self):
        assert _p1_receipt_view(self._receipt(start_time=None)) is None


class TestProbeAndJudge:
    """探测顺序：回执优先，取不到才回退归档件。"""

    @staticmethod
    def _client(handler):
        import httpx

        return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://relay")

    @pytest.mark.asyncio
    async def test_done_receipt_verifies_without_reading_archive_dir(self):
        import httpx

        calls: list[str] = []
        created = datetime.now(UTC) - timedelta(minutes=20)
        receipt = {
            "found": True,
            "upload_id": "u-9",
            "status": "done",
            "file_size": 120,
            "uploaded_size": 120,
            "received_chunks": 3,
            "chunk_total": 3,
            "start_time": (created + timedelta(minutes=5)).isoformat(),
        }

        async def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            if request.url.path == "/upload_progress":
                return httpx.Response(200, json=receipt)
            return httpx.Response(500)

        task = _task(created)
        async with self._client(handler) as http:
            source, archive, view = await _probe_p1_archive(
                lc_route.SangforRelayClient("http://relay"), http, "Q1.zip", "t-1"
            )

        assert source == "upload_progress"
        assert view["state"] == "done"
        verified, reason, size, _ = _judge_p1_probe(
            task,
            archive=archive,
            receipt_view=view,
            expected_size=120,
            expected_size_trustworthy=True,
        )
        assert (verified, reason, size) == (True, "verified", 120)
        assert "/archive_status" not in calls

    @pytest.mark.asyncio
    async def test_uploading_session_never_falls_back_to_stale_archive(self):
        """上传中绝不看归档目录：那里挂着的可能是上一批次的旧包。"""
        import httpx

        calls: list[str] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            if request.url.path == "/upload_progress":
                return httpx.Response(
                    200,
                    json={
                        "found": True,
                        "upload_id": "u-2",
                        "status": "uploading",
                        "file_size": 120,
                        "uploaded_size": 40,
                        "start_time": datetime.now(UTC).isoformat(),
                    },
                )
            return httpx.Response(200, json={"found": True, "size": 4_478_420_847})

        task = _task()
        async with self._client(handler) as http:
            source, archive, view = await _probe_p1_archive(
                lc_route.SangforRelayClient("http://relay"), http, "Q1.zip", "t-1"
            )

        assert source == "upload_progress"
        assert archive == {}
        verified, reason, _, _ = _judge_p1_probe(
            task, archive=archive, receipt_view=view,
            expected_size=120, expected_size_trustworthy=True,
        )
        assert (verified, reason) == (False, "archive_missing")
        assert "/archive_status" not in calls

    @pytest.mark.asyncio
    async def test_unsupported_endpoint_falls_back_to_archive_status(self):
        import httpx

        called: list[str] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            called.append(request.url.path)
            if request.url.path == "/upload_progress":
                return httpx.Response(404, json={"error": "not found"})
            return httpx.Response(
                200, json={"found": True, "size": 120, "mtime": datetime.now(UTC).timestamp()}
            )

        task = _task()
        async with self._client(handler) as http:
            source, archive, view = await _probe_p1_archive(
                lc_route.SangforRelayClient("http://relay"), http, "Q1.zip", "t-1"
            )

        assert source == "archive_status"
        assert view is None
        assert called == ["/upload_progress", "/archive_status"]
        verified, reason, size, _ = _judge_p1_probe(
            task,
            archive=archive,
            receipt_view=view,
            expected_size=120,
            expected_size_trustworthy=True,
        )
        assert (verified, reason, size) == (True, "verified", 120)

    @pytest.mark.asyncio
    async def test_receipt_older_than_task_is_stale(self):
        """回执属于上一批次（会话在任务创建前就开始）：不能算本次结果。"""
        import httpx

        created = datetime.now(UTC) - timedelta(minutes=10)

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "found": True,
                    "upload_id": "u-3",
                    "status": "done",
                    "file_size": 120,
                    "uploaded_size": 120,
                    "received_chunks": 3,
                    "chunk_total": 3,
                    "start_time": (created - timedelta(hours=3)).isoformat(),
                },
            )

        task = _task(created)
        async with self._client(handler) as http:
            source, archive, view = await _probe_p1_archive(
                lc_route.SangforRelayClient("http://relay"), http, "Q1.zip", "t-1"
            )
        verified, reason, _, _ = _judge_p1_probe(
            task, archive=archive, receipt_view=view,
            expected_size=120, expected_size_trustworthy=True,
        )
        assert (verified, reason) == (False, "stale_archive")

    @pytest.mark.asyncio
    async def test_receipt_smaller_than_confirmed_size_is_truncation(self):
        import httpx

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "found": True,
                    "upload_id": "u-4",
                    "status": "done",
                    "file_size": 80,
                    "uploaded_size": 80,
                    "start_time": datetime.now(UTC).isoformat(),
                },
            )

        task = _task()
        async with self._client(handler) as http:
            source, archive, view = await _probe_p1_archive(
                lc_route.SangforRelayClient("http://relay"), http, "Q1.zip", "t-1"
            )
        verified, reason, size, _ = _judge_p1_probe(
            task, archive=archive, receipt_view=view,
            expected_size=120, expected_size_trustworthy=True,
        )
        assert (verified, reason, size) == (False, "size_mismatch", 80)


class _FakeDB:
    def __init__(self, rows: list[dict]):
        self._rows = rows

    async def get_session(self):
        session = SimpleNamespace()

        async def _execute(_stmt, _params=None):
            return SimpleNamespace(mappings=lambda: SimpleNamespace(all=lambda: self._rows))

        session.execute = _execute
        yield session


class TestBackgroundP1Reconcile:
    """服务端异步 P1 归档复核（Q2026112501591 / Q2026111900904 回归守护）。"""

    @pytest.mark.asyncio
    async def test_promotes_delivered_when_5888_finishes_merging(self, monkeypatch):
        """5888 滞后合并完成后，后台复核自动转正为 delivered（Q2026112501591）。"""
        task_id = uuid4()
        created = datetime.now(UTC) - timedelta(minutes=20)
        row = {
            "task_id": task_id,
            "case_id": "Q2026112501591",
            "archive_size": 5_656_413_749,
            "delivery_status": "none",
            "created_at": created,
            "trace_id": "lc-Q2026112501591-test",
            "detail": {"p1_archive_check": {"verified": False, "reason": "archive_missing"}},
        }
        monkeypatch.setattr(lc_route, "database_manager", _FakeDB([row]))
        monkeypatch.setattr(
            lc_route,
            "get_p1_ticket_service",
            lambda: SimpleNamespace(enabled=True, issue=lambda cid, fn: "t-valid"),
        )
        monkeypatch.setattr(lc_route.settings, "LOG_COLLECT_RELAY_URL", "http://127.0.0.1:5888")

        async def _fake_archive_status(self, _http, _filename, *, ticket=""):
            assert ticket == "t-valid"
            return {
                "found": True,
                "size": 5_656_413_749,
                "mtime": (created + timedelta(minutes=10)).timestamp(),
                "stored_name": "Q2026112501591.zip",
            }

        monkeypatch.setattr(lc_route.SangforRelayClient, "archive_status", _fake_archive_status)
        # 旧版 5888 没有回执端点：后台复核必须回退归档件而不是把核验打成失败。
        monkeypatch.setattr(
            lc_route.SangforRelayClient,
            "upload_progress",
            AsyncMock(return_value=("unsupported", None)),
        )
        updated: list[tuple] = []

        async def _fake_update(self, tid, patch, *, server_authoritative=False):
            updated.append((tid, patch, server_authoritative))

        monkeypatch.setattr(lc_route.LogCollectService, "update_task", _fake_update)

        count = await reconcile_p1_archives_once()
        assert count == 1
        assert len(updated) == 1
        tid, patch, auth = updated[0]
        assert tid == task_id
        assert auth is True
        assert patch.delivery_status == "delivered"
        assert patch.detail["p1_archive_check"]["source"] == "background_reconcile"

    @pytest.mark.asyncio
    async def test_receipt_detail_does_not_override_reconcile_source(self, monkeypatch):
        """回执细节可以并入快照，但 source 必须仍标识"后台复核"这条写入路径。"""
        task_id = uuid4()
        created = datetime.now(UTC) - timedelta(minutes=20)
        row = {
            "task_id": task_id,
            "case_id": "Q2026112501592",
            "archive_size": 120,
            "delivery_status": "none",
            "created_at": created,
            "trace_id": "lc-Q2026112501592-test",
            "detail": {"p1_archive_check": {"verified": False, "reason": "archive_missing"}},
        }
        monkeypatch.setattr(lc_route, "database_manager", _FakeDB([row]))
        monkeypatch.setattr(
            lc_route,
            "get_p1_ticket_service",
            lambda: SimpleNamespace(enabled=True, issue=lambda cid, fn: "t-valid"),
        )
        monkeypatch.setattr(lc_route.settings, "LOG_COLLECT_RELAY_URL", "http://127.0.0.1:5888")
        monkeypatch.setattr(
            lc_route.SangforRelayClient,
            "upload_progress",
            AsyncMock(
                return_value=(
                    "ok",
                    {
                        "found": True,
                        "upload_id": "u-bg",
                        "status": "done",
                        "file_size": 120,
                        "uploaded_size": 120,
                        "received_chunks": 1,
                        "chunk_total": 1,
                        "start_time": (created + timedelta(minutes=5)).isoformat(),
                    },
                )
            ),
        )
        updated: list[tuple] = []

        async def _fake_update(self, tid, patch, *, server_authoritative=False):
            updated.append((tid, patch, server_authoritative))

        monkeypatch.setattr(lc_route.LogCollectService, "update_task", _fake_update)

        count = await reconcile_p1_archives_once()
        assert count == 1
        check = updated[0][1].detail["p1_archive_check"]
        assert check["source"] == "background_reconcile"
        assert check["evidence_source"] == "upload_progress"
        assert check["upload_id"] == "u-bg"

    @pytest.mark.asyncio
    async def test_negative_result_does_not_refresh_checked_at(self, monkeypatch):
        """5888 仍无归档时绝不写库刷新 checked_at，避免真实丢包单（Q2026111900904）永不过期。"""
        task_id = uuid4()
        row = {
            "task_id": task_id,
            "case_id": "Q2026111900904",
            "archive_size": None,
            "delivery_status": "none",
            "created_at": datetime.now(UTC) - timedelta(minutes=15),
            "trace_id": "lc-Q2026111900904-test",
            "detail": {
                "p1_archive_check": {"verified": False, "reason": "archive_missing", "expected_size": 24467180672}
            },
        }
        monkeypatch.setattr(lc_route, "database_manager", _FakeDB([row]))
        monkeypatch.setattr(
            lc_route,
            "get_p1_ticket_service",
            lambda: SimpleNamespace(enabled=True, issue=lambda cid, fn: "t-valid"),
        )
        monkeypatch.setattr(lc_route.settings, "LOG_COLLECT_RELAY_URL", "http://127.0.0.1:5888")
        monkeypatch.setattr(
            lc_route.SangforRelayClient,
            "archive_status",
            AsyncMock(return_value={"found": False, "status": "missing"}),
        )
        monkeypatch.setattr(
            lc_route.SangforRelayClient,
            "upload_progress",
            AsyncMock(return_value=("unsupported", None)),
        )
        update_mock = AsyncMock()
        monkeypatch.setattr(lc_route.LogCollectService, "update_task", update_mock)

        count = await reconcile_p1_archives_once()
        assert count == 0
        update_mock.assert_not_awaited()
