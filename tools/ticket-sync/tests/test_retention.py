"""保留期判据回归测试（2026-10-31）。

生产背景：`cleanup_expired` 上线后一个文件都没删过，
`ticket_sync_expired_removed_total` 恒为 0。根因是历史目录的
`.sync-meta.json` 只有 bytes/hasLogs、没有 completedAt，保留期只能回退目录
mtime；而 mtime 会被外部写入刷新（DSH 往产物里写报告、人工 touch），
于是一份 2026-10-21 的 64.3 GiB 产物在 10-31 仍被判成"刚同步完"，永不逾期。

这里锁住四件事：
1. 首次对账必须把回退值固化成显式 completedAt；
2. 固化之后目录再被写入，完成时间不再跟着漂移；
3. 脏字段（0 / 布尔 / 未来）不能让产物被判成立即过期而误删；
4. 正在同步的工单既不被固化，也不能因为本轮"先跳过"就丢掉记账行。
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.maintenance import (
    SYNC_META_NAME,
    cleanup_expired,
    read_sync_meta,
    reconcile_artifacts,
    write_sync_meta,
)
from app.store import Store

TZ = timezone(timedelta(hours=8))
TICKET_ID = "Q2026010100001"


def _make_ticket(root: Path, name: str, size: int = 100) -> Path:
    """造一个已发布产物：内容只有一个文件，meta 由调用方补。"""
    ticket = root / name
    ticket.mkdir()
    (ticket / "log.bin").write_bytes(b"x" * size)
    return ticket


def _set_age(ticket: Path, age_days: float, now: datetime) -> float:
    """把目录 mtime 回拨到 age_days 天前。

    必须放在写 meta 之后：`write_sync_meta` 会新建文件、把目录 mtime 刷成现在，
    先回拨再写等于白拨——这正是生产里"回填后必须恢复 mtime"的同一个坑。
    """
    stamp = (now - timedelta(days=age_days)).timestamp()
    os.utime(ticket, (stamp, stamp))
    return stamp


def _write_raw_meta(ticket: Path, payload: dict) -> None:
    """绕过 write_sync_meta 直接落盘，用于构造脏 meta。"""
    (ticket / SYNC_META_NAME).write_text(json.dumps(payload), encoding="utf-8")


class LegacyCompletedAtTest(unittest.TestCase):
    """历史目录缺 completedAt 时的固化行为。"""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="ticket-sync-retention-"))
        self.now = datetime(2026, 10, 31, 12, 0, tzinfo=TZ)
        self.store = Store(self.root / ".ticket-sync.db")

    def tearDown(self) -> None:
        self.store.close()
        shutil.rmtree(self.root, ignore_errors=True)

    def test_missing_completed_at_is_frozen_from_mtime(self) -> None:
        """缺字段时必须写成显式事实，而不是每次判过期都去回退 mtime。"""
        ticket = _make_ticket(self.root, TICKET_ID)
        write_sync_meta(ticket, bytes=100, hasLogs=True)  # 刻意不带 completedAt
        stamp = _set_age(ticket, 10, self.now)
        self.assertNotIn("completedAt", read_sync_meta(ticket))

        reconcile_artifacts(self.root, self.store)

        self.assertAlmostEqual(read_sync_meta(ticket)["completedAt"], stamp, delta=1)
        # 补写 meta 后目录 mtime 必须还原，否则"固化值"与"mtime 信号"互相打架
        self.assertAlmostEqual(ticket.stat().st_mtime, stamp, delta=1)

    def test_unchanged_ticket_without_completed_at_is_still_frozen(self) -> None:
        """mtime 没变过也必须再固化一次，否则存量永远走不到固化分支。

        真实事故：第一版补丁部署后 37 个目录里 0 个被固化、`removed` 依然恒为 0。
        原因是它们的 mtime 从上次对账起就没变过，被"未变化就跳过"直接挡在门外，
        而"完成时间是否已固化"当时并不在跳过判据里。
        """
        ticket = _make_ticket(self.root, TICKET_ID)
        write_sync_meta(ticket, bytes=100, hasLogs=True)
        stamp = _set_age(ticket, 10, self.now)
        reconcile_artifacts(self.root, self.store)  # 先建立"已记账"状态

        # 抹掉 completedAt 并还原 mtime，造出"上一版留下的存量 meta"
        meta = read_sync_meta(ticket)
        meta.pop("completedAt", None)
        (ticket / SYNC_META_NAME).write_text(json.dumps(meta), encoding="utf-8")
        os.utime(ticket, (stamp, stamp))

        added, updated, _ = reconcile_artifacts(self.root, self.store)

        self.assertEqual((added, updated), (0, 1))
        self.assertAlmostEqual(read_sync_meta(ticket)["completedAt"], stamp, delta=1)

    def test_frozen_completed_at_ignores_later_writes(self) -> None:
        """固化后目录再被写（DSH 写报告），完成时间不跟着漂移——本次缺陷的核心。"""
        ticket = _make_ticket(self.root, TICKET_ID)
        write_sync_meta(ticket, bytes=100, hasLogs=True)
        _set_age(ticket, 10, self.now)
        reconcile_artifacts(self.root, self.store)
        frozen = read_sync_meta(ticket)["completedAt"]

        # 模拟外部在产物里写了个报告文件，目录 mtime 被刷成"现在"
        (ticket / "report.md").write_text("diag", encoding="utf-8")
        touched = self.now.timestamp()
        os.utime(ticket, (touched, touched))

        reconcile_artifacts(self.root, self.store)

        self.assertEqual(read_sync_meta(ticket)["completedAt"], frozen)
        self.assertLess(frozen, touched - 86400)

    def test_legacy_ticket_past_retention_is_actually_removed(self) -> None:
        """端到端：按 mtime 固化后，超过保留期的产物必须真的被删掉。"""
        ticket = _make_ticket(self.root, TICKET_ID)
        write_sync_meta(ticket, bytes=100, hasLogs=False)
        _set_age(ticket, 10, self.now)

        reconcile_artifacts(self.root, self.store)
        removed = cleanup_expired(self.root, self.now, retention_days=7)

        self.assertEqual(removed, 1)
        self.assertFalse(ticket.exists())

    def test_bool_completed_at_does_not_make_ticket_instantly_expired(self) -> None:
        """`completedAt: true` 是 int 子类，不排除就会被当成 1970 年而误删。"""
        ticket = _make_ticket(self.root, TICKET_ID)
        _write_raw_meta(ticket, {"bytes": 100, "hasLogs": True, "completedAt": True})
        stamp = _set_age(ticket, 1, self.now)

        reconcile_artifacts(self.root, self.store)

        self.assertAlmostEqual(self.store.artifact_map()[TICKET_ID]["completed_at"], stamp, delta=1)
        # 1 天前的产物不该被 7 天保留期删掉
        self.assertEqual(cleanup_expired(self.root, self.now, retention_days=7), 0)
        self.assertTrue(ticket.exists())

    def test_zero_completed_at_is_replaced(self) -> None:
        """0 时间戳同样会判成立即过期，必须按"字段不存在"处理。"""
        ticket = _make_ticket(self.root, TICKET_ID)
        _write_raw_meta(ticket, {"bytes": 100, "hasLogs": True, "completedAt": 0})
        stamp = _set_age(ticket, 1, self.now)

        reconcile_artifacts(self.root, self.store)

        self.assertAlmostEqual(read_sync_meta(ticket)["completedAt"], stamp, delta=1)

    def test_active_ticket_is_skipped_but_keeps_its_row(self) -> None:
        """正在同步的目录不固化；同时不能因为"本轮跳过"被当成目录消失。"""
        ticket = _make_ticket(self.root, TICKET_ID)
        write_sync_meta(ticket, bytes=100, hasLogs=True, completedAt=self.now.timestamp())
        _set_age(ticket, 1, self.now)
        reconcile_artifacts(self.root, self.store)
        self.assertIn(TICKET_ID, self.store.artifact_map())

        # 目录正在被写：mtime 变了，但该工单处于活跃状态
        later = self.now.timestamp() + 60
        os.utime(ticket, (later, later))

        self.assertEqual(reconcile_artifacts(self.root, self.store, {TICKET_ID}), (0, 0, 0))
        self.assertIn(TICKET_ID, self.store.artifact_map())
        self.assertEqual(self.store.artifact_total(), 100)


if __name__ == "__main__":
    unittest.main()
