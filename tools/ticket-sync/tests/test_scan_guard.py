"""自动发现的候选筛选与限流（2026-11-01 事故回归）。

事故经过：把扫描窗口从「当天」放宽成「最近 7 天」之后，升级后第一轮巡检就把
95 个归档里 57 个**从未同步过**的历史工单全部入队，一次消耗 766 GiB 磁盘，
不得不停服止血并清理。

根因是把两件事混为一谈：
- 扫描窗口要解决的是「昨天失败的任务今天还能重试」；
- 而窗口内的历史存量**本来就不该同步**——客户从没要求同步它们。

所以候选必须分两路：当天归档照常；更大的窗口只在「这台机器上有失败记录」时
才用，且结果还要用 `retries` 过滤。再加单轮入队硬上限兜底。
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from app.config import Config
from app.fetcher import RemoteArchive
from app.main import MAX_AUTO_JOBS_PER_SCAN, JobRegistry, RetryState, _auto_discover

TZ = ZoneInfo("Asia/Shanghai")
TODAY = "Q2026110100001"


def _archive(ticket_id: str, now: datetime) -> RemoteArchive:
    return RemoteArchive(ticket_id, f"/sf/log/{ticket_id}/{ticket_id}.zip", 1024, now.timestamp())


class AutoDiscoverTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="ticket-sync-scan-")
        os.environ["TICKET_ROOT"] = self.root
        os.environ["TICKET_ARCHIVE_PASSWORD"] = "test-password"
        os.environ["TICKET_AUTO_SYNC_ENABLED"] = "false"
        self.cfg = Config.from_env()
        self.now = datetime(2026, 11, 1, 12, 0, tzinfo=TZ)
        self.registry = JobRegistry(self.cfg)
        self.calls: list[int] = []
        self.enqueued: list[str] = []

    async def asyncTearDown(self) -> None:
        self.registry.close()
        for key in ("TICKET_ROOT", "TICKET_ARCHIVE_PASSWORD", "TICKET_AUTO_SYNC_ENABLED"):
            os.environ.pop(key, None)
        shutil.rmtree(self.root, ignore_errors=True)

    def _install(self, *, today: list[str], recent: list[str], returns_job: bool) -> None:
        """装桩：记录被问过的窗口天数、被入队的工单号。"""
        calls = self.calls
        enqueued = self.enqueued

        async def fake_scan(cfg, days=1, now=None):
            calls.append(days)
            ids = today if days == 1 else recent
            return [_archive(ticket_id, now) for ticket_id in ids]

        async def fake_create(self, archive):
            enqueued.append(archive.ticket_id)
            return object() if returns_job else None

        self._patches = [
            patch("app.main.list_recent_archives", fake_scan),
            patch.object(JobRegistry, "create_auto", fake_create),
        ]
        for patcher in self._patches:
            patcher.start()

    def _uninstall(self) -> None:
        for patcher in self._patches:
            patcher.stop()

    async def test_history_without_failure_record_is_never_enqueued(self) -> None:
        """这是本次事故的核心断言：窗口内的历史存量不该被补拉。"""
        self._install(today=[TODAY], recent=[TODAY, "Q2026010100009"], returns_job=True)
        try:
            await _auto_discover(self.cfg, self.registry, self.now)
        finally:
            self._uninstall()

        # 没有失败记录时只扫当天，不额外回看
        self.assertEqual(self.calls, [1])
        self.assertEqual(self.enqueued, [TODAY])

    async def test_failed_ticket_is_looked_back_and_retried(self) -> None:
        """昨天失败的任务必须能被找回，否则退避阶梯等于白设。"""
        self.registry._retries["Q2026010100009"] = RetryState(failures=1)
        self._install(
            today=[TODAY],
            recent=[TODAY, "Q2026010100009", "Q2026010100010"],
            returns_job=True,
        )
        try:
            await _auto_discover(self.cfg, self.registry, self.now)
        finally:
            self._uninstall()

        self.assertEqual(self.calls, [1, 7])
        # 只有"有失败记录"的那个被补入队，另一个历史工单仍被排除
        self.assertEqual(self.enqueued, [TODAY, "Q2026010100009"])

    async def test_single_round_is_capped(self) -> None:
        """判据再出错，也不能一次性放大成几十个任务。"""
        many = [f"Q20261101000{index:02d}" for index in range(10)]
        self._install(today=many, recent=[], returns_job=True)
        try:
            created = await _auto_discover(self.cfg, self.registry, self.now)
        finally:
            self._uninstall()

        self.assertEqual(created, MAX_AUTO_JOBS_PER_SCAN)
        self.assertEqual(len(self.enqueued), MAX_AUTO_JOBS_PER_SCAN)

    async def test_skipped_candidates_do_not_consume_the_cap(self) -> None:
        """create_auto 返回 None（目录已存在等）不该占用限流额度。"""
        many = [f"Q20261101000{index:02d}" for index in range(8)]
        self._install(today=many, recent=[], returns_job=False)
        try:
            created = await _auto_discover(self.cfg, self.registry, self.now)
        finally:
            self._uninstall()

        self.assertEqual(created, 0)
        self.assertEqual(len(self.enqueued), len(many))


if __name__ == "__main__":
    unittest.main()
