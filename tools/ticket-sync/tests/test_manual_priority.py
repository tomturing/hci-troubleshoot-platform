"""手动任务独立通道（2026-11-01）。

问题：手动与自动任务共用同一个 `Semaphore(TICKET_CONCURRENCY)`。一轮巡检入队
5 个 60 GiB 级的包之后，用户在 admin-ui 输入工单号点击"同步日志"，任务只能
排在队尾 `queued`，最长要等两批跑完（数小时）——点击的反馈与点击的意图严重脱节。

结论：手动任务代表用户当下的一次显式判断，必须立即开始，不占自动巡检的队列
位置；自动任务仍受信号量约束（数量由扫描判据决定，判据一错就会同时开满带宽）。
同工单串行不受影响：仍由 `RUNNING_PHASES` 在入队口返回 409。
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.config import Config
from app.fetcher import RemoteArchive
from app.main import ORIGIN_AUTO, ORIGIN_MANUAL, JobConflict, JobRegistry

AUTO_TICKET = "Q2026110100001"
AUTO_TICKET_2 = "Q2026110100002"
MANUAL_TICKET = "Q2026110100003"


def _make_config(root: Path) -> Config:
    """并发上限 1：只要手动任务能开始，就证明它没在等自动任务的槽位。"""
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


def _archive(ticket_id: str) -> RemoteArchive:
    return RemoteArchive(ticket_id, f"/sf/log/{ticket_id}/{ticket_id}.zip", 1024, 1.0)


class ManualPriorityTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="ticket-sync-priority-"))
        self.cfg = _make_config(self.root)
        self.registry = JobRegistry(self.cfg)
        # 记录"真正开始拉取"的工单号，并用一个事件把已经进入执行的任务钉住。
        self.started: list[str] = []
        self.release = asyncio.Event()

        async def fake_fetch(ticket_id, cfg, work_dir, job_id, on_progress=None):
            self.started.append(ticket_id)
            await self.release.wait()
            raise AssertionError("本用例不应跑到拉取完成之后")

        async def no_remote_entries(*_args, **_kwargs):
            """跳过下载前的归档内容探测：它只走 ssh，测试里用不上。"""
            return []

        self._patches = [
            patch("app.main.fetch", fake_fetch),
            patch("app.main.list_remote_entries", no_remote_entries),
        ]
        for patcher in self._patches:
            patcher.start()

    async def asyncTearDown(self) -> None:
        pending = [
            job.task
            for job in list(self.registry.jobs.values())
            if job.task is not None and not job.task.done()
        ]
        # 先 cancel 再放行：让任务从 wait() 里以 CancelledError 退出，
        # 而不是走 fake_fetch 的 AssertionError 把状态写成 failed。
        for task in pending:
            task.cancel()
        self.release.set()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        for patcher in self._patches:
            patcher.stop()
        self.registry.close()
        shutil.rmtree(self.root, ignore_errors=True)

    async def _settle(self) -> None:
        """让 create_task 推进到拉取阶段（或停在信号量上）。"""
        for _ in range(10):
            await asyncio.sleep(0)

    async def test_manual_job_starts_while_auto_job_holds_the_slot(self) -> None:
        auto_job = await self.registry.create_auto(_archive(AUTO_TICKET))
        self.assertIsNotNone(auto_job)
        await self._settle()
        self.assertEqual(self.started, [AUTO_TICKET])

        # 第二个自动任务必须排队：这是限流仍然生效的对照组。
        blocked = await self.registry.create_auto(_archive(AUTO_TICKET_2))
        self.assertIsNotNone(blocked)
        await self._settle()
        self.assertEqual(self.started, [AUTO_TICKET], "自动任务不得绕过并发上限")

        manual_job = await self.registry.create(MANUAL_TICKET, "req-manual")
        self.assertEqual(manual_job.origin, ORIGIN_MANUAL)
        await self._settle()

        self.assertIn(MANUAL_TICKET, self.started, "手动任务必须立即开始，不排自动任务的队")
        self.assertEqual(manual_job.phase.value, "fetching")

    async def test_auto_job_origin_is_marked(self) -> None:
        job = await self.registry.create_auto(_archive(AUTO_TICKET))
        self.assertIsNotNone(job)
        self.assertEqual(job.origin, ORIGIN_AUTO)
        self.assertEqual(job.to_payload()["origin"], ORIGIN_AUTO)

    async def test_same_ticket_still_conflicts(self) -> None:
        """绕过并发闸门不等于绕过同工单串行：两个 rsync 抢同一目录会互相破坏。"""
        await self.registry.create(MANUAL_TICKET, "req-1")
        await self._settle()

        with self.assertRaises(JobConflict):
            await self.registry.create(MANUAL_TICKET, "req-2")

    async def test_origin_survives_restart(self) -> None:
        """来源要落库：重启后 /artifacts 仍能回答「这任务是人工点出来的吗」。"""
        job = await self.registry.create(MANUAL_TICKET, "req-1")
        self.assertEqual(job.origin, ORIGIN_MANUAL)

        # 不关原连接：SQLite WAL 允许第二个连接读同一文件，而关掉原连接会让
        # 本用例的 teardown 重复 close。这里只验证"落库后能被新进程读回"。
        reopened = JobRegistry(self.cfg)
        try:
            self.assertEqual(reopened.get(MANUAL_TICKET).origin, ORIGIN_MANUAL)
        finally:
            reopened.close()


if __name__ == "__main__":
    unittest.main()
