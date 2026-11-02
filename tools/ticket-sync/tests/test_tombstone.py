"""墓碑记录与退避持久化测试（2026-11-01）。

两个必须同时成立的结论：

1. **墓碑**：自动扫描窗口放宽到 7 天之后，"按保留期删掉的产物"会被再次扫到。
   没有"这个工单同步过"的记录，就变成"删了又拉"的无限循环。墓碑只挡自动扫描，
   不挡手动 POST——用户显式点击代表新的判断。

2. **退避持久化**：退避原本只存内存，重启即清零；而每次重试都是完整重拉，
   一次部署重启就能让几十 GiB 的坏包再拉一遍。
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from app.config import Config
from app.fetcher import RemoteArchive, list_recent_archives
from app.main import ORIGIN_AUTO, TOMBSTONE_RETENTION_DAYS, JobRegistry
from app.models import Phase

TZ = ZoneInfo("Asia/Shanghai")
TICKET_ID = "Q2026010100001"


def _make_config(root: str) -> Config:
    os.environ["TICKET_ROOT"] = root
    os.environ["TICKET_ARCHIVE_PASSWORD"] = "test-password"
    os.environ["TICKET_AUTO_SYNC_ENABLED"] = "false"
    return Config.from_env()


def _clear_env() -> None:
    for key in ("TICKET_ROOT", "TICKET_ARCHIVE_PASSWORD", "TICKET_AUTO_SYNC_ENABLED"):
        os.environ.pop(key, None)


def _archive(ticket_id: str, now: datetime) -> RemoteArchive:
    return RemoteArchive(
        ticket_id,
        f"/sf/log/{ticket_id}/{ticket_id}.zip",
        123,
        now.timestamp(),
    )


class TombstoneTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="ticket-sync-tombstone-")
        self.cfg = _make_config(self.root)
        # 不能钉死具体日期：`store.add_tombstone` 的 removed_at 用的是
        # `datetime.now()`（产物被删的真实时刻），而 prune 的 cutoff 由测试传入的
        # "现在"推导。两者一旦用不同基准，断言就随日历漂移——曾经写成
        # `datetime(2026, 11, 1, 12, 0)`，真实时间越过 2026-11-02 12:00 后
        # `cutoff = self.now + (31-30) 天` 被真实 removed_at 反超，该用例每天
        # 12:00 之后必然删 0 行。以真实时钟为基准即可保持"相对过期"语义。
        self.now = datetime.now(TZ)
        self.registry = JobRegistry(self.cfg)

    async def asyncTearDown(self) -> None:
        self.registry.close()
        _clear_env()
        shutil.rmtree(self.root, ignore_errors=True)

    def _publish_then_forget(self) -> None:
        """造出"同步过、随后被保留期清理"的完整状态。"""
        final_dir = Path(self.root) / TICKET_ID
        final_dir.mkdir(parents=True, exist_ok=True)
        self.registry._store.upsert_artifact(
            TICKET_ID, 100, 1, True, self.now.timestamp(), self.now.timestamp()
        )
        self.registry._forget_ticket(TICKET_ID)
        shutil.rmtree(final_dir, ignore_errors=True)

    async def test_forget_ticket_leaves_tombstone(self) -> None:
        self._publish_then_forget()

        self.assertIn(TICKET_ID, self.registry._tombstones)
        self.assertIn(TICKET_ID, self.registry._store.tombstone_map())
        # 记账行必须真的删掉，否则容量统计会算进不存在的产物
        self.assertIsNone(self.registry._store.get_artifact(TICKET_ID))

    async def test_auto_scan_skips_tombstoned_ticket(self) -> None:
        """这是本次修复的核心：删掉之后不能再被自动扫回来。"""
        self._publish_then_forget()

        job = await self.registry.create_auto(_archive(TICKET_ID, self.now))

        self.assertIsNone(job)
        self.assertNotIn(TICKET_ID, self.registry.jobs)

    async def test_manual_create_ignores_tombstone(self) -> None:
        """用户显式点击代表新的判断，墓碑不该拦手动重拉。"""
        self._publish_then_forget()

        job = self.registry._create_locked(TICKET_ID, ORIGIN_AUTO)

        self.assertEqual(job.phase, Phase.QUEUED)
        self.registry.jobs.pop(TICKET_ID, None)
        self.registry._history.pop(TICKET_ID, None)

    async def test_tombstone_survives_restart(self) -> None:
        """重启后仍要知道"这个工单同步过"，否则第一轮巡检就把它拉回来。"""
        self._publish_then_forget()
        self.registry.close()

        self.registry = JobRegistry(self.cfg)

        self.assertIn(TICKET_ID, self.registry._tombstones)
        self.assertIsNone(await self.registry.create_auto(_archive(TICKET_ID, self.now)))

    async def test_prune_drops_tombstone_and_unblocks_scan(self) -> None:
        """墓碑过期后应该放行，否则一个工单被永久判死。"""
        self._publish_then_forget()
        # 与保留期常量绑定而不是写死天数：调整保留期时用例不该跟着改
        later = self.now + timedelta(days=TOMBSTONE_RETENTION_DAYS + 1)

        self.assertEqual(self.registry.prune_tombstones(later), 1)

        self.assertNotIn(TICKET_ID, self.registry._tombstones)

    async def test_prune_keeps_tombstone_before_expiry(self) -> None:
        """未过期的墓碑不能被删：删了等于放行，"删了又拉"的循环会回来。

        与上一条构成边界两侧——只测"过期会删"时，把 cutoff 写反的实现
        （永远删不到）也能被误判为通过。
        """
        self._publish_then_forget()
        soon = self.now + timedelta(days=TOMBSTONE_RETENTION_DAYS - 1)

        self.assertEqual(self.registry.prune_tombstones(soon), 0)
        self.assertIn(TICKET_ID, self.registry._tombstones)


class RetryPersistenceTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="ticket-sync-retry-")
        self.cfg = _make_config(self.root)

    async def asyncTearDown(self) -> None:
        _clear_env()
        shutil.rmtree(self.root, ignore_errors=True)

    async def test_retry_state_survives_restart(self) -> None:
        registry = JobRegistry(self.cfg)
        registry._note_failure(TICKET_ID)
        failures = registry._retries[TICKET_ID].failures
        next_at = registry._retries[TICKET_ID].next_retry_at
        registry.close()

        restarted = JobRegistry(self.cfg)
        try:
            state = restarted._retries.get(TICKET_ID)
            self.assertIsNotNone(state, "退避状态没有跨重启保留")
            self.assertEqual(state.failures, failures)
            self.assertEqual(state.next_retry_at, next_at)
        finally:
            restarted.close()

    async def test_success_clears_retry_state(self) -> None:
        registry = JobRegistry(self.cfg)
        try:
            registry._note_failure(TICKET_ID)
            registry._clear_retry(TICKET_ID)

            self.assertNotIn(TICKET_ID, registry._retries)
            self.assertEqual(registry._store.load_retries(), {})
        finally:
            registry.close()


class ScanWindowTest(unittest.IsolatedAsyncioTestCase):
    async def test_window_covers_multiple_days(self) -> None:
        """窗口必须覆盖前几天，否则昨天失败的任务今天再也不会被自动发现。"""
        with tempfile.TemporaryDirectory() as raw:
            cfg = _make_config(raw)
            now = datetime(2026, 10, 31, 12, 0, tzinfo=TZ)

            class FakeProcess:
                returncode = 0

                async def communicate(self):
                    return (b"", b"")

            launcher = AsyncMock(return_value=FakeProcess())
            with patch("app.fetcher.asyncio.create_subprocess_exec", launcher):
                await list_recent_archives(cfg, 7, now)

            command = launcher.await_args.args[-1]
            self.assertIn("-newermt '2026-10-25 00:00:00'", command)
            self.assertIn("! -newermt '2026-11-01 00:00:00'", command)
            _clear_env()


if __name__ == "__main__":
    unittest.main()
