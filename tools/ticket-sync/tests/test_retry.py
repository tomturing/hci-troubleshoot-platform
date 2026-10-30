"""失败退避测试：自动巡检不能对必然失败的工单无限重拉。

生产背景：老逻辑只判断"本地目录是否存在"，失败后目录不存在 → 下一轮
30 秒巡检再次入队，实测单工单 192 次、累计约 450 GiB 无效流量。
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from app.config import Config
from app.fetcher import RemoteArchive
from app.main import RETRY_BACKOFF_SECONDS, JobRegistry, RetryState, _now


def _make_config(root: Path) -> Config:
    return Config(
        source_host="192.168.0.2",
        source_port=22468,
        source_user="acli",
        ssh_key=Path("/nonexistent/id_ed25519"),
        archive_password="test-password",
        root=root,
        listen_host="127.0.0.1",
        listen_port=18080,
        max_archive_bytes=20 * 1024**3,
        max_extract_bytes=400 * 1024**3,
        min_free_bytes=0,
        max_files=200_000,
        max_depth=8,
        concurrency=1,
        auto_sync_enabled=False,
        auto_sync_interval_seconds=30,
        retention_days=7,
    )


class RetryBackoffTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="ticket-sync-retry-"))
        self.cfg = _make_config(self.root)
        self.registry = JobRegistry(self.cfg)
        # 只验证入队判定，不真正发起 fetch。
        self.registry._start = lambda job, request_id: None  # type: ignore[method-assign]
        self.archive = RemoteArchive(
            ticket_id="Q2026010100001",
            path="/sf/log/Q2026010100001/Q2026010100001.zip",
            size=1024,
            mtime=1.0,
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_backoff_ladder(self) -> None:
        state = RetryState()
        delays = []
        for _ in range(len(RETRY_BACKOFF_SECONDS) + 2):
            delays.append(state.delay_for_next())
            state.failures += 1
        self.assertEqual(delays[:4], [600, 1800, 7200, 21600])
        # 阶梯到头后固定在上限，不能无限增长。
        self.assertEqual(delays[-1], 21600)

    async def test_auto_scan_skips_ticket_inside_backoff_window(self) -> None:
        self.registry._note_failure(self.archive.ticket_id)

        job = await self.registry.create_auto(self.archive)

        self.assertIsNone(job)
        state = self.registry._retries[self.archive.ticket_id]
        self.assertEqual(state.failures, 1)
        self.assertGreater(state.next_retry_at, _now())

    async def test_auto_scan_resumes_after_window(self) -> None:
        self.registry._note_failure(self.archive.ticket_id)
        # 把窗口挪到过去，等价于"退避到期"。
        self.registry._retries[self.archive.ticket_id].next_retry_at = _now() - timedelta(seconds=1)

        job = await self.registry.create_auto(self.archive)

        self.assertIsNotNone(job)
        self.assertEqual(job.ticket_id, self.archive.ticket_id)

    async def test_success_clears_backoff(self) -> None:
        self.registry._note_failure(self.archive.ticket_id)
        (self.root / self.archive.ticket_id).mkdir()

        job = await self.registry.create_auto(self.archive)

        self.assertIsNone(job)
        self.assertNotIn(self.archive.ticket_id, self.registry._retries)


if __name__ == "__main__":
    unittest.main()
