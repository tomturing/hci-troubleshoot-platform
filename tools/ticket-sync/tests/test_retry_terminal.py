"""重试终态测试：退避阶梯封顶不等于无限重试。

生产背景（2026-11-01）：`RETRY_BACKOFF_SECONDS` 用 `min(failures, 3)` 取档，
到 6 小时后再也不会增长，于是 6 个必然失败的工单（12.2 GiB）在两小时一次的
循环里空转，永久占用并发槽；只要 `retries` 非空，扫描还一直走 7 天宽窗口。
另一个反例是 `non-log-payload`：已经判定"不是日志包"，却仍被反复重试。
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from app.config import Config
from app.fetcher import RemoteArchive
from app.main import MAX_RETRY_ATTEMPTS, JobRegistry
from app.models import ErrorCode


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


class RetryTerminalTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="ticket-sync-terminal-"))
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
        self.registry.close()
        shutil.rmtree(self.root, ignore_errors=True)

    def _fail_repeatedly(self, times: int, code: ErrorCode = ErrorCode.EXTRACT_FAILED) -> None:
        for _ in range(times):
            self.registry._note_failure(self.archive.ticket_id, code)

    async def test_stops_after_max_attempts(self) -> None:
        self._fail_repeatedly(MAX_RETRY_ATTEMPTS)

        state = self.registry._retries[self.archive.ticket_id]
        self.assertTrue(state.exhausted)
        # 终态工单不再进入 7 天补扫，否则宽窗口会被它永远撑着。
        self.assertNotIn(self.archive.ticket_id, self.registry.retry_ticket_ids())
        self.assertIsNone(await self.registry.create_auto(self.archive))

    async def test_before_max_attempts_it_still_retries(self) -> None:
        """上限之前必须保持可重试，否则退避阶梯形同虚设。"""
        self._fail_repeatedly(MAX_RETRY_ATTEMPTS - 1)

        state = self.registry._retries[self.archive.ticket_id]
        self.assertFalse(state.exhausted)
        self.assertIn(self.archive.ticket_id, self.registry.retry_ticket_ids())

    async def test_deterministic_error_is_terminal_immediately(self) -> None:
        """non-log-payload 重试多少次都还是转储包，第一次就该停。"""
        self.registry._note_failure(self.archive.ticket_id, ErrorCode.NON_LOG_PAYLOAD)

        state = self.registry._retries[self.archive.ticket_id]
        self.assertTrue(state.exhausted)
        self.assertIsNone(await self.registry.create_auto(self.archive))

    async def test_archive_corrupt_is_terminal(self) -> None:
        """归档损坏是数据事实：重拉多少次都是同一份坏数据。

        实测 Q2026102802029 的内层 gzip 流被截断，`unzip -t` 通过、两端 md5 一致，
        这种包不可能靠重试变好。
        """
        self.registry._note_failure(self.archive.ticket_id, ErrorCode.ARCHIVE_CORRUPT)

        self.assertTrue(self.registry._retries[self.archive.ticket_id].exhausted)
        self.assertIsNone(await self.registry.create_auto(self.archive))

    async def test_terminal_state_survives_restart(self) -> None:
        """终态必须跨重启：否则每次部署都会把坏包重新拉一遍。"""
        self._fail_repeatedly(MAX_RETRY_ATTEMPTS)
        self.registry.close()

        restarted = JobRegistry(self.cfg)
        restarted._start = lambda job, request_id: None  # type: ignore[method-assign]
        try:
            state = restarted._retries[self.archive.ticket_id]
            self.assertTrue(state.exhausted)
            self.assertIsNone(await restarted.create_auto(self.archive))
        finally:
            restarted.close()

    async def test_manual_create_is_not_blocked_by_terminal_state(self) -> None:
        """终态只约束自动扫描：人点按钮代表新的判断，必须放行。"""
        self._fail_repeatedly(MAX_RETRY_ATTEMPTS)

        job = await self.registry.create(self.archive.ticket_id)

        self.assertEqual(job.ticket_id, self.archive.ticket_id)


if __name__ == "__main__":
    unittest.main()
