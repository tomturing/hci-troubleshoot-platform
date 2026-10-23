"""API 层测试：校验、冲突、取消、健康检查。

任务执行路径用 mock 替换，避免测试依赖真实 rsync 与归档密码。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from httpx import ASGITransport, AsyncClient

from app.main import app


async def _hang(*_args, **_kwargs):
    """模拟一个长时间运行的任务，用于验证冲突与取消。"""
    await asyncio.sleep(30)
    raise AssertionError("被取消的任务不应执行到这里")


class ApiTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="ticket-sync-test-")
        os.environ["TICKET_ROOT"] = self.root
        os.environ["TICKET_ARCHIVE_PASSWORD"] = "test-password"

        # 手动驱动 lifespan：httpx 的 ASGITransport 不会替我们执行 startup/shutdown。
        self._lifespan = app.router.lifespan_context(app)
        await self._lifespan.__aenter__()

        self.client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        await self._lifespan.__aexit__(None, None, None)
        os.environ.pop("TICKET_ROOT", None)
        os.environ.pop("TICKET_ARCHIVE_PASSWORD", None)
        shutil.rmtree(self.root, ignore_errors=True)

    async def test_health(self) -> None:
        response = await self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "ok")
        self.assertGreater(body["diskFreeBytes"], 0)
        self.assertEqual(body["activeJobs"], 0)

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
        with patch("app.main.fetch", side_effect=_hang):
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
        with patch("app.main.fetch", side_effect=_hang):
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
        with patch("app.main.fetch", side_effect=_hang):
            await self.client.post("/jobs", json={"ticketId": "Q2026010100004"})
            await asyncio.sleep(0)
            await self.client.delete("/jobs/Q2026010100004")

        # 已 cancelled 的任务再次取消应当返回 204，而不是 404 或 500
        again = await self.client.delete("/jobs/Q2026010100004")
        self.assertEqual(again.status_code, 204)


if __name__ == "__main__":
    unittest.main()
