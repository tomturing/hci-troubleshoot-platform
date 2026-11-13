"""API 层测试：校验、冲突、取消、健康检查。

任务执行路径用 mock 替换，避免测试依赖真实 rsync 与归档密码。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from httpx import ASGITransport, AsyncClient

from app.fetcher import RemoteArchive
from app.main import app


async def _list_archives(cfg, ticket_id):
    return [RemoteArchive(ticket_id, f"/sf/log/{ticket_id}/{ticket_id}.zip", 3, 1.0)]


async def _hang(*_args, **_kwargs):
    """模拟一个长时间运行的任务，用于验证冲突与取消。"""
    await asyncio.sleep(30)
    raise AssertionError("被取消的任务不应执行到这里")


class ApiTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="ticket-sync-test-")
        os.environ["TICKET_ROOT"] = self.root
        os.environ["TICKET_ARCHIVE_PASSWORD"] = "test-password"
        # 本用例验证的是任务生命周期，不是磁盘水位：宿主机余量不该决定测试结果
        # （默认硬阈值 250 GiB，小盘/CI 机器必然低于它而让任务在入口就被熔断）。
        os.environ["TICKET_MIN_FREE_BYTES"] = "0"

        # 手动驱动 lifespan：httpx 的 ASGITransport 不会替我们执行 startup/shutdown。
        self._lifespan = app.router.lifespan_context(app)
        await self._lifespan.__aenter__()

        self.client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        await self._lifespan.__aexit__(None, None, None)
        for key in ("TICKET_ROOT", "TICKET_ARCHIVE_PASSWORD", "TICKET_MIN_FREE_BYTES"):
            os.environ.pop(key, None)
        shutil.rmtree(self.root, ignore_errors=True)

    async def test_health(self) -> None:
        response = await self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "ok")
        self.assertGreater(body["diskFreeBytes"], 0)
        self.assertEqual(body["activeJobs"], 0)

    async def test_metrics_exposes_alertable_series(self) -> None:
        """指标是给告警用的，必须有磁盘余量与维护耗时这两条。"""
        response = await self.client.get("/metrics")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/plain", response.headers["content-type"])
        body = response.text
        self.assertIn("ticket_sync_disk_free_bytes", body)
        self.assertIn("ticket_sync_active_jobs", body)
        self.assertIn("ticket_sync_maintenance_duration_seconds", body)

    async def test_list_artifacts_returns_local_ticket_directory(self) -> None:
        ticket_dir = Path(self.root) / "Q2026010100001"
        ticket_dir.mkdir()
        (ticket_dir / "log_list.json").write_text("{}", encoding="utf-8")

        response = await self.client.get("/artifacts")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["items"][0]["ticketId"], "Q2026010100001")
        self.assertEqual(response.json()["items"][0]["phase"], "ready")
        self.assertTrue(response.json()["items"][0]["directoryReady"])

    async def test_invalid_ticket_id_rejected(self) -> None:
        response = await self.client.post("/jobs", json={"ticketId": "../etc/passwd"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "invalid-ticket-id")

    async def test_overlong_ticket_id_rejected(self) -> None:
        # 不设请求体长度上限，就是为了让超长输入同样落到 400 invalid-ticket-id，
        # 而不是混入 FastAPI 的 422。
        response = await self.client.post("/jobs", json={"ticketId": "Q" + "1" * 40})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "invalid-ticket-id")

    async def test_unknown_job_returns_404(self) -> None:
        response = await self.client.get("/jobs/Q2026010100000")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"], "not-found")

    async def test_create_then_conflict(self) -> None:
        with (
            patch("app.main.list_ticket_archives", _list_archives),
            patch("app.main.fetch", side_effect=_hang),
        ):
            first = await self.client.post("/jobs", json={"ticketId": "Q2026010100001"})
            self.assertEqual(first.status_code, 202)
            self.assertEqual(first.json()["phase"], "queued")

            # 让 create_task 真正开始执行，phase 才会离开 queued
            await asyncio.sleep(0)

            second = await self.client.post("/jobs", json={"ticketId": "Q2026010100001"})
            self.assertEqual(second.status_code, 409, "同一工单并发提交必须被拒")
            self.assertEqual(second.json()["error"], "job-running")

            await self.client.delete("/jobs/Q2026010100001")

    async def test_cancel_running_job(self) -> None:
        with (
            patch("app.main.list_ticket_archives", _list_archives),
            patch("app.main.fetch", side_effect=_hang),
        ):
            await self.client.post("/jobs", json={"ticketId": "Q2026010100002"})
            await asyncio.sleep(0)

            cancelled = await self.client.delete("/jobs/Q2026010100002")
            self.assertEqual(cancelled.status_code, 204)

            state = await self.client.get("/jobs/Q2026010100002")
            self.assertEqual(state.json()["phase"], "cancelled")

    async def test_cancel_unknown_returns_404(self) -> None:
        response = await self.client.delete("/jobs/Q2026010100003")
        self.assertEqual(response.status_code, 404)

    async def test_cancel_finished_job_is_idempotent(self) -> None:
        with (
            patch("app.main.list_ticket_archives", _list_archives),
            patch("app.main.fetch", side_effect=_hang),
        ):
            await self.client.post("/jobs", json={"ticketId": "Q2026010100004"})
            await asyncio.sleep(0)
            await self.client.delete("/jobs/Q2026010100004")

        # 已 cancelled 的任务再次取消应当返回 204，而不是 404 或 500
        again = await self.client.delete("/jobs/Q2026010100004")
        self.assertEqual(again.status_code, 204)

    async def test_cleanup_events_use_camel_case_contract(self) -> None:
        """清理流水的字段名必须与前端契约一致（camelCase）。

        store 返回的是 SQLite 行（snake_case）。端点若直接透出，前端读
        `row.ticketId` / `row.freeBytes` 全是 undefined，「工单」与「当时余量」
        两列会静默变空白——恰好把"谁被删了、当时盘还剩多少"这个最关键的信息
        藏起来。这条断言就是为了防止它再退化。
        """
        store = app.state.registry.store
        store.add_cleanup_event(
            "retention",
            1_700_000_000.0,
            ticket_id="Q2026010100009",
            bytes_=3 * 1024**3,
            free_bytes=500 * 1024**3,
            detail="age_days=4.52 retention_days=3",
        )

        response = await self.client.get("/cleanup-events?limit=5")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["items"])
        event = body["items"][0]
        self.assertEqual(event["ticketId"], "Q2026010100009")
        self.assertEqual(event["freeBytes"], 500 * 1024**3)
        self.assertIsInstance(event["occurredAt"], str)
        # 不能同时保留 snake_case，否则契约含糊，前端仍可能读错
        self.assertNotIn("ticket_id", event)
        self.assertNotIn("free_bytes", event)
        self.assertNotIn("occurred_at", event)

        # 阈值同样是 camelCase，界面直接读这几个键
        thresholds = body["thresholds"]
        self.assertGreater(thresholds["evictFreeBytes"], thresholds["minFreeBytes"])

    async def test_artifacts_expose_root_and_cleaned_phase(self) -> None:
        """/artifacts 必须回传真实根目录，并把已清理工单标成 cleaned。

        前端曾硬编码 `/data/tickets`，而实际根目录是 `/sf/tickets`——路径写错
        会直接把用户引到错误位置去清理。cleaned 阶段则保证"删了"不会被误读成
        "从没同步过"。
        """
        store = app.state.registry.store
        ticket = "Q2026010100008"
        # 刻意不建目录：cleaned 表示"同步成功过、产物已被清理"，有目录就该报 ready
        store.add_tombstone(ticket, 1_700_000_000.0)

        response = await self.client.get("/artifacts")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["root"], self.root)
        cleaned = [item for item in body["items"] if item["ticketId"] == ticket]
        self.assertTrue(cleaned, "已清理工单必须出现在列表里")
        self.assertEqual(cleaned[0]["phase"], "cleaned")


if __name__ == "__main__":
    unittest.main()
