"""放弃/恢复自动重试接口测试。

背景（2026-11-02）：源端归档损坏的工单（Q2026102802029）只能等 6 次退避才转终态，
运维现场只好直接 `sqlite3 update retries`。但 `retries` 表只是镜像，运行中进程的
内存状态才是权威——实测改完立刻被写回（6 → 4），必须停服才能生效。
接口的存在意义就是把这件事做对：内存与库同时改，不需要停服。
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from httpx import ASGITransport, AsyncClient

from app.fetcher import RemoteArchive
from app.main import MAX_RETRY_ATTEMPTS, app
from app.models import ErrorCode

TICKET_ID = "Q2026110200001"


class AbandonApiTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="ticket-sync-abandon-")
        os.environ["TICKET_ROOT"] = self.root
        os.environ["TICKET_ARCHIVE_PASSWORD"] = "test-password"
        os.environ["TICKET_AUTO_SYNC_ENABLED"] = "false"

        self._lifespan = app.router.lifespan_context(app)
        await self._lifespan.__aenter__()

        self.registry = app.state.registry
        # 不真正发起 fetch：本测试只验证退避状态的读写。
        self.registry._start = lambda job, request_id: None  # type: ignore[method-assign]
        self.archive = RemoteArchive(
            ticket_id=TICKET_ID,
            path=f"/sf/log/{TICKET_ID}/{TICKET_ID}.zip",
            size=1024,
            mtime=1.0,
        )
        self.client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        await self._lifespan.__aexit__(None, None, None)
        for key in ("TICKET_ROOT", "TICKET_ARCHIVE_PASSWORD", "TICKET_AUTO_SYNC_ENABLED"):
            os.environ.pop(key, None)
        shutil.rmtree(self.root, ignore_errors=True)

    async def test_abandon_marks_terminal_and_persists(self) -> None:
        response = await self.client.post(f"/jobs/{TICKET_ID}/abandon")

        self.assertEqual(response.status_code, 202)
        payload = response.json()
        self.assertEqual(payload["ticketId"], TICKET_ID)
        self.assertTrue(payload["retry"]["exhausted"])
        self.assertIsNone(payload["retry"]["nextRetryAt"])
        self.assertEqual(payload["retry"]["failures"], MAX_RETRY_ATTEMPTS)
        # 必须落库，否则重启就白放弃了。
        self.assertIsNone(self.registry._store.load_retries()[TICKET_ID]["next_retry_at"])

    async def test_abandoned_ticket_is_no_longer_auto_enqueued(self) -> None:
        await self.client.post(f"/jobs/{TICKET_ID}/abandon")

        self.assertNotIn(TICKET_ID, self.registry.retry_ticket_ids())
        self.assertIsNone(await self.registry.create_auto(self.archive))

    async def test_manual_job_still_allowed_after_abandon(self) -> None:
        """放弃的是自动重试，不是同步能力。"""
        await self.client.post(f"/jobs/{TICKET_ID}/abandon")

        job = await self.registry.create(TICKET_ID)

        self.assertEqual(job.ticket_id, TICKET_ID)

    async def test_resume_clears_terminal_state(self) -> None:
        await self.client.post(f"/jobs/{TICKET_ID}/abandon")

        response = await self.client.delete(f"/jobs/{TICKET_ID}/abandon")

        self.assertEqual(response.status_code, 204)
        self.assertNotIn(TICKET_ID, self.registry._retries)
        self.assertIsNotNone(await self.registry.create_auto(self.archive))

    async def test_failure_after_abandon_keeps_terminal_state(self) -> None:
        """放弃之后那次在跑的任务失败了，也不能把它拉回退避循环。"""
        await self.client.post(f"/jobs/{TICKET_ID}/abandon")

        self.registry._note_failure(TICKET_ID, ErrorCode.EXTRACT_FAILED)

        state = self.registry._retries[TICKET_ID]
        self.assertTrue(state.exhausted)
        self.assertIsNone(state.next_retry_at)

    async def test_invalid_ticket_id_is_rejected(self) -> None:
        response = await self.client.post("/jobs/not-a-ticket/abandon")

        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
