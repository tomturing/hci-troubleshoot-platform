"""容量治理测试：保留期是时间维度，容量是空间维度，两者都要管。

生产背景：实测客户单日推来 5 个 50~65 GiB 工单，7 天保留期足以填满 1.2T 磁盘，
而 min_free_bytes 只会拒绝新任务、不会自愈，盘满后所有工单都同步不了。
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.maintenance import enforce_capacity

TZ = timezone(timedelta(hours=8))


def _make_ticket(root: Path, name: str, size: int, age_days: float, now: datetime) -> Path:
    ticket = root / name
    ticket.mkdir()
    (ticket / "log.bin").write_bytes(b"x" * size)
    stamp = (now - timedelta(days=age_days)).timestamp()
    os.utime(ticket, (stamp, stamp))
    return ticket


class EnforceCapacityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="ticket-sync-cap-"))
        self.now = datetime(2026, 10, 30, 12, 0, tzinfo=TZ)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_oldest_tickets_removed_until_under_limit(self) -> None:
        _make_ticket(self.root, "Q2026010100001", 100, 10, self.now)
        _make_ticket(self.root, "Q2026010100002", 100, 8, self.now)
        _make_ticket(self.root, "Q2026010100003", 100, 6, self.now)

        removed, freed = enforce_capacity(self.root, 150, 3, self.now)

        self.assertEqual(removed, 2)
        self.assertEqual(freed, 200)
        self.assertFalse((self.root / "Q2026010100001").exists())
        self.assertFalse((self.root / "Q2026010100002").exists())
        self.assertTrue((self.root / "Q2026010100003").exists())

    def test_recent_tickets_are_protected(self) -> None:
        """不足最短保留天数的工单即使超限也不能删（避免刚同步就被清）。"""
        _make_ticket(self.root, "Q2026010100001", 500, 1, self.now)
        _make_ticket(self.root, "Q2026010100002", 500, 2, self.now)

        removed, freed = enforce_capacity(self.root, 100, 3, self.now)

        self.assertEqual((removed, freed), (0, 0))
        self.assertTrue((self.root / "Q2026010100001").exists())
        self.assertTrue((self.root / "Q2026010100002").exists())

    def test_active_ticket_is_never_removed(self) -> None:
        _make_ticket(self.root, "Q2026010100001", 300, 9, self.now)
        _make_ticket(self.root, "Q2026010100002", 300, 8, self.now)

        removed, _ = enforce_capacity(self.root, 300, 3, self.now, {"Q2026010100001"})

        self.assertEqual(removed, 1)
        self.assertTrue((self.root / "Q2026010100001").exists())
        self.assertFalse((self.root / "Q2026010100002").exists())

    def test_noop_when_under_limit(self) -> None:
        _make_ticket(self.root, "Q2026010100001", 100, 10, self.now)

        self.assertEqual(enforce_capacity(self.root, 10_000, 3, self.now), (0, 0))

    def test_non_ticket_directories_ignored(self) -> None:
        """knowledge/workflows 等宿主机目录不属于服务资产，绝不能删。"""
        extra = self.root / "knowledge"
        extra.mkdir()
        (extra / "a.md").write_bytes(b"x" * 500)
        old = self.now.timestamp() - 30 * 86400
        os.utime(extra, (old, old))

        removed, _ = enforce_capacity(self.root, 0, 3, self.now)

        self.assertEqual(removed, 0)
        self.assertTrue(extra.exists())


if __name__ == "__main__":
    unittest.main()
