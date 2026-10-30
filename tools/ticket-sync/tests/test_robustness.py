"""清理判据与载荷识别的回归测试（2026-10-31）。

生产背景：
- 2026-10-31 00:00:07 → 00:03:35，服务完全静默 3.5 分钟：容量检查在协程里
  同步遍历 550 GiB / 44 万文件，把事件循环钉死。现在统计改为「优先读发布时
  写入的 meta，缺失才扫一次」，且整体移入线程池。
- 保留期原本用目录 mtime 判断。mtime 会被 DSH 在产物里写报告、人工 touch 改写，
  既不反映"何时同步完"，也可能让刚同步的工单被误删。
- Q2026102101911 下载 16.4 GiB、解压 65 GiB，产物只有一个 kasi/kasi.dump，
  旧载荷规则只认 vmdumpcore/.core，全部放行。
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.fetcher import looks_like_non_log_bundle
from app.maintenance import (
    cleanup_expired,
    enforce_capacity,
    has_usable_logs,
    write_sync_meta,
)

TZ = timezone(timedelta(hours=8))


def _age(path: Path, age_days: float, now: datetime) -> None:
    """设置目录 mtime。写 meta 会刷新目录 mtime，所以要在写 meta 之后调用。"""
    stamp = (now - timedelta(days=age_days)).timestamp()
    os.utime(path, (stamp, stamp))


def _make_ticket(root: Path, name: str, size: int, age_days: float, now: datetime) -> Path:
    ticket = root / name
    ticket.mkdir()
    (ticket / "log.bin").write_bytes(b"x" * size)
    _age(ticket, age_days, now)
    return ticket


class SyncMetaRetentionTest(unittest.TestCase):
    """保留期判据：显式 completedAt 优先，目录 mtime 只做回退。"""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="ticket-sync-retention-"))
        self.now = datetime(2026, 10, 31, 12, 0, tzinfo=TZ)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_completed_at_overrides_dir_mtime(self) -> None:
        """目录看起来是 1 天前，但 meta 说 10 天前同步 → 必须清理。"""
        ticket = _make_ticket(self.root, "Q2026010100001", 100, 1, self.now)
        write_sync_meta(ticket, completedAt=(self.now - timedelta(days=10)).timestamp())

        self.assertEqual(cleanup_expired(self.root, self.now, 7), 1)
        self.assertFalse(ticket.exists())

    def test_fresh_sync_survives_old_mtime(self) -> None:
        """反例：mtime 被外部改成 30 天前，但刚同步完 → 不能删。"""
        ticket = _make_ticket(self.root, "Q2026010100001", 100, 30, self.now)
        write_sync_meta(ticket, completedAt=self.now.timestamp())

        self.assertEqual(cleanup_expired(self.root, self.now, 7), 0)
        self.assertTrue(ticket.exists())

    def test_legacy_dir_without_meta_falls_back_to_mtime(self) -> None:
        """历史目录没有 meta，行为不能变，否则升级当天会把存量全删或全留。"""
        ticket = _make_ticket(self.root, "Q2026010100001", 100, 10, self.now)

        self.assertEqual(cleanup_expired(self.root, self.now, 7), 1)
        self.assertFalse(ticket.exists())


class CapacityEvictionOrderTest(unittest.TestCase):
    """容量淘汰顺序：非日志载荷先出局。"""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="ticket-sync-evict-"))
        self.now = datetime(2026, 10, 31, 12, 0, tzinfo=TZ)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_non_log_payload_evicted_before_older_logs(self) -> None:
        """一个较新的 65 GiB dump 不该挤掉更旧但可用的日志工单。"""
        logs = _make_ticket(self.root, "Q2026010100001", 100, 10, self.now)
        write_sync_meta(logs, hasLogs=True)
        _age(logs, 10, self.now)
        dump = _make_ticket(self.root, "Q2026010100002", 100, 5, self.now)
        write_sync_meta(dump, hasLogs=False)
        _age(dump, 5, self.now)

        # 每个工单约 118 字节（100 字节载荷 + meta 文件），上限 200 表示只删一个。
        removed, _ = enforce_capacity(self.root, 200, 3, self.now)

        self.assertEqual(removed, 1)
        self.assertFalse(dump.exists())
        self.assertTrue(logs.exists())

    def test_falls_back_to_oldest_when_both_have_logs(self) -> None:
        old = _make_ticket(self.root, "Q2026010100001", 100, 10, self.now)
        write_sync_meta(old, hasLogs=True)
        _age(old, 10, self.now)
        new = _make_ticket(self.root, "Q2026010100002", 100, 5, self.now)
        write_sync_meta(new, hasLogs=True)
        _age(new, 5, self.now)

        removed, _ = enforce_capacity(self.root, 200, 3, self.now)

        self.assertEqual(removed, 1)
        self.assertFalse(old.exists())
        self.assertTrue(new.exists())

    def test_size_backfill_keeps_original_mtime(self) -> None:
        """回填尺寸会新增 meta 文件；若顺带刷新 mtime，历史目录就永远不过期。"""
        ticket = _make_ticket(self.root, "Q2026010100001", 100, 10, self.now)
        before = ticket.stat().st_mtime

        enforce_capacity(self.root, 10**9, 3, self.now)

        self.assertAlmostEqual(ticket.stat().st_mtime, before, places=1)
        self.assertTrue((ticket / ".sync-meta.json").is_file())

    def test_bytes_cached_into_meta(self) -> None:
        """首次统计后回填尺寸，避免每天重扫 44 万文件。"""
        ticket = _make_ticket(self.root, "Q2026010100001", 1234, 1, self.now)
        enforce_capacity(self.root, 10**9, 3, self.now)

        meta = (ticket / ".sync-meta.json").read_text(encoding="utf-8")
        self.assertIn('"bytes": 1234', meta)


class UsableLogsTest(unittest.TestCase):
    """「流程跑通」不等于「产物可用」，需要独立判据。"""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="ticket-sync-logs-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_log_list_is_usable(self) -> None:
        (self.root / "log_list.json").write_text("{}", encoding="utf-8")
        self.assertTrue(has_usable_logs(self.root))

    def test_sp_directory_is_usable(self) -> None:
        (self.root / "sp_10.12.23.7").mkdir()
        self.assertTrue(has_usable_logs(self.root))

    def test_nested_ticket_dir_is_probed(self) -> None:
        """产物会套一层与工单号同名的目录（Q.../Q.../log_list.json）。"""
        inner = self.root / "Q2026010100001"
        inner.mkdir()
        (inner / "log_list.json").write_text("{}", encoding="utf-8")
        self.assertTrue(has_usable_logs(self.root))

    def test_dump_only_payload_is_not_usable(self) -> None:
        inner = self.root / "Q2026010100001" / "kasi"
        inner.mkdir(parents=True)
        (inner / "kasi.dump").write_bytes(b"x" * 10)
        self.assertFalse(has_usable_logs(self.root))


class NonLogPayloadDetectionTest(unittest.TestCase):
    """转储特征必须覆盖 .dump —— 实测单包 65 GiB。"""

    def test_kasi_dump_is_detected(self) -> None:
        self.assertTrue(looks_like_non_log_bundle(["kasi/kasi.dump"]))

    def test_vmcore_variants_are_detected(self) -> None:
        for name in ("vmcore", "vmdumpcore", "kdump-2026.img", "pid1.core", "crash-2026.dmp"):
            self.assertTrue(looks_like_non_log_bundle([name]), name)

    def test_bundle_with_log_list_is_never_skipped(self) -> None:
        self.assertFalse(looks_like_non_log_bundle(["kasi/kasi.dump", "log_list.json"]))

    def test_tar_gz_bundle_is_not_skipped(self) -> None:
        self.assertFalse(looks_like_non_log_bundle(["sp_1.1.1.1.tar.gz", "kasi.dump"]))


if __name__ == "__main__":
    unittest.main()
