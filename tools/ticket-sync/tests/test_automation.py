"""自动扫描、并发互斥与本地保留期清理测试。"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from app.config import Config
from app.fetcher import RemoteArchive, _parse_remote_archives, list_today_archives
from app.main import JobRegistry
from app.maintenance import cleanup_expired


class RemoteArchiveParserTest(unittest.TestCase):
    def test_accepts_only_top_level_ticket_archive(self) -> None:
        output = (
            "/sf/log/Q2026102900001/Q2026102900001.zip\t123\t1793232000.5\n"
            "/sf/log/Q2026102900002/other.zip\t456\t1793232001.5\n"
            "/sf/log/Q2026102900003/nested/a.zip\t789\t1793232002.5\n"
            "/sf/log/not-a-ticket/not-a-ticket.zip\t1\t1793232003.5\n"
        )

        archives = _parse_remote_archives(output)

        self.assertEqual(
            archives,
            [RemoteArchive("Q2026102900001", "/sf/log/Q2026102900001/Q2026102900001.zip", 123, 1793232000.5)],
        )


class RetentionTest(unittest.TestCase):
    def test_removes_expired_ticket_directory_but_keeps_active_and_non_ticket(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            now = datetime(2026, 10, 29, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
            old = root / "Q2026100100001"
            old.mkdir()
            active = root / "Q2026100100002"
            active.mkdir()
            other = root / "notes"
            other.mkdir()
            old_trash = root / ".trash" / "Q2026100100003-20261001-120000"
            old_trash.mkdir(parents=True)
            old_stamp = now.timestamp() - 8 * 24 * 60 * 60
            os.utime(old, (old_stamp, old_stamp))
            os.utime(active, (old_stamp, old_stamp))
            os.utime(old_trash, (old_stamp, old_stamp))

            removed = cleanup_expired(root, now, 7, {active.name})

            self.assertEqual(removed, 2)
            self.assertFalse(old.exists())
            self.assertTrue(active.exists())
            self.assertTrue(other.exists())
            self.assertFalse(old_trash.exists())


class AutoRegistryTest(unittest.IsolatedAsyncioTestCase):
    async def test_remote_scan_uses_local_day_boundary_and_parses_output(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            os.environ["TICKET_ROOT"] = raw
            cfg = Config.from_env()
            now = datetime(2026, 10, 29, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

            class FakeProcess:
                returncode = 0

                async def communicate(self):
                    return (
                        b"/sf/log/Q2026102900001/Q2026102900001.zip\t123\t1793232000.5\n",
                        b"",
                    )

            launcher = AsyncMock(return_value=FakeProcess())
            with patch("app.fetcher.asyncio.create_subprocess_exec", launcher):
                archives = await list_today_archives(cfg, now)

            self.assertEqual([item.ticket_id for item in archives], ["Q2026102900001"])
            command = launcher.await_args.args[-1]
            self.assertIn("-newermt '2026-10-29 00:00:00'", command)
            self.assertIn("! -newermt '2026-10-30 00:00:00'", command)
            os.environ.pop("TICKET_ROOT", None)

    async def test_same_archive_is_not_enqueued_twice_while_first_task_runs(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            os.environ["TICKET_ROOT"] = raw
            cfg = Config.from_env()
            registry = JobRegistry(cfg)
            archive = RemoteArchive(
                "Q2026102900001",
                "/sf/log/Q2026102900001/Q2026102900001.zip",
                123,
                datetime.now().timestamp(),
            )
            running = asyncio.Event()

            async def hold(_registry: JobRegistry, job) -> None:
                running.set()
                await asyncio.Event().wait()

            with patch.object(JobRegistry, "_run", new=hold):
                first = await registry.create_auto(archive)
                self.assertIsNotNone(first)
                await running.wait()
                second = await registry.create_auto(archive)

                self.assertIsNone(second)
                await registry.cancel(archive.ticket_id)
            os.environ.pop("TICKET_ROOT", None)
            shutil.rmtree(root, ignore_errors=True)

    async def test_existing_local_directory_is_not_rescheduled(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            local = root / "Q2026102900001"
            local.mkdir()
            os.environ["TICKET_ROOT"] = raw
            cfg = Config.from_env()
            registry = JobRegistry(cfg)
            local_mtime = datetime.now().timestamp()
            os.utime(local, (local_mtime, local_mtime))
            archive = RemoteArchive(
                "Q2026102900001",
                "/sf/log/Q2026102900001/Q2026102900001.zip",
                123,
                local_mtime - 1,
            )

            self.assertIsNone(await registry.create_auto(archive))
            self.assertEqual(registry.active_count(), 0)
            os.environ.pop("TICKET_ROOT", None)
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
