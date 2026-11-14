"""清理机制与磁盘水位的回归测试。

生产背景（2026-11-12 全栈中断）：清理只在北京时间凌晨那一次循环里跑，白天涌入的
几百 GiB 无人淘汰，把与 k3s 共享的 2.1T 盘写满，kubelet 驱逐整节点 Pod。
这批测试锁住三条不可回退的契约：

1. 清理必须能「半分钟一轮」地跑，且每轮的结果都要落库留痕；
2. 磁盘余量（唯一真值）必须能单独触发淘汰，且软阈值严格大于硬阈值；
3. 删除后的工单必须以「已清理」出现在列表里，而不是凭空消失。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from app.config import Config
from app.maintenance import (
    cleanup_expired,
    reconcile_missing,
    run_cleanup_cycle,
    write_sync_meta,
)
from app.models import ErrorCode, Phase, TicketError
from app.publisher import publish
from app.store import (
    CLEANUP_KIND_CAPACITY,
    CLEANUP_KIND_RECONCILE,
    CLEANUP_KIND_RETENTION,
    Store,
)
from app.waterline import ensure_waterline

TZ = ZoneInfo("Asia/Shanghai")
GIB = 1024**3


def _publish_dir(root: Path, ticket_id: str, *, age_days: float, size: int, now: datetime) -> Path:
    """造一个「已发布」的工单目录：目录 + 带 completedAt/bytes 的同步元数据。"""
    directory = root / ticket_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "log_list.json").write_text("{}", encoding="utf-8")
    write_sync_meta(
        directory,
        completedAt=(now - timedelta(days=age_days)).timestamp(),
        bytes=size,
        files=1,
        hasLogs=True,
    )
    return directory


class WaterlineTest(unittest.TestCase):
    def test_rejects_when_free_below_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            with patch("app.waterline.shutil.disk_usage") as usage:
                usage.return_value = SimpleNamespace(free=10 * GIB)
                with self.assertRaises(TicketError) as ctx:
                    ensure_waterline(Path(raw), 250 * GIB, stage="fetch")
            self.assertEqual(ctx.exception.code, ErrorCode.LOW_DISK)
            # 用户看到的必须是可行动的数字，不能只有一句"空间不足"。
            self.assertIn("10.0 GiB", ctx.exception.message)
            self.assertIn("250 GiB", ctx.exception.message)

    def test_passes_when_free_above_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            with patch("app.waterline.shutil.disk_usage") as usage:
                usage.return_value = SimpleNamespace(free=600 * GIB)
                ensure_waterline(Path(raw), 250 * GIB, stage="fetch")

    def test_probe_failure_allows_task(self) -> None:
        """探测失败必须放行：拦的是"确定写不下"，不是"可能写不下"。"""
        with tempfile.TemporaryDirectory() as raw:
            with patch("app.waterline.shutil.disk_usage", side_effect=OSError("boom")):
                ensure_waterline(Path(raw), 250 * GIB, stage="fetch")


class ConfigGuardTest(unittest.TestCase):
    def _cfg(self, **overrides):
        base = dict(
            source_host="192.168.0.2",
            source_port=22468,
            source_user="acli",
            ssh_key=Path("/tmp/k"),
            archive_password="x",
            root=Path("/tmp/x"),
            listen_host="127.0.0.1",
            listen_port=18080,
            max_archive_bytes=20 * GIB,
            max_extract_bytes=400 * GIB,
            min_free_bytes=250 * GIB,
            evict_free_bytes=400 * GIB,
            max_files=500_000,
            max_depth=12,
            concurrency=2,
            auto_sync_enabled=True,
            auto_sync_interval_seconds=30,
            retention_days=3,
            min_keep_days=1,
        )
        base.update(overrides)
        return Config(**base)

    def test_evict_threshold_must_exceed_min_threshold(self) -> None:
        """淘汰阈值不高于熔断阈值时，服务必须起不来。

        两者反了不会在运行时报错，只会表现成"每次都失败、清理永远来不及"——
        这正是 2026-11-12 事故的形态，现场只能靠读 journal 才发现。
        """
        from app.config import _validate_waterline

        with self.assertRaises(SystemExit):
            _validate_waterline(self._cfg(evict_free_bytes=200 * GIB, min_free_bytes=250 * GIB))
        with self.assertRaises(SystemExit):
            _validate_waterline(self._cfg(evict_free_bytes=250 * GIB, min_free_bytes=250 * GIB))
        _validate_waterline(self._cfg())

    def test_retention_must_exceed_min_keep(self) -> None:
        """保留期不大于最短保留天数时，保留期永远删 0 个（实测 expired_removed=0）。"""
        from app.config import _validate_waterline

        with self.assertRaises(SystemExit):
            _validate_waterline(self._cfg(retention_days=3, min_keep_days=3))
        _validate_waterline(self._cfg(retention_days=3, min_keep_days=1))


class CleanupCycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.root = base / "tickets"
        self.root.mkdir()
        self.tmp_root = self.root / ".tmp"
        self.tmp_root.mkdir()
        self.trash = self.root / ".trash"
        self.trash.mkdir()
        self.now = datetime(2026, 11, 12, 16, 30, tzinfo=TZ)
        self.store = Store(base / "state.db")

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def _cycle(
        self,
        *,
        free: int = 900 * GIB,
        retention_days: int = 3,
        min_keep_days: int = 1,
        active_ticket_ids: frozenset[str] = frozenset(),
    ):
        with patch("app.maintenance.shutil.disk_usage") as usage:
            usage.return_value = SimpleNamespace(free=free, total=2 * 1024 * GIB, used=0)
            return run_cleanup_cycle(
                root=self.root,
                tmp_root=self.tmp_root,
                trash_dir=self.trash,
                now=self.now,
                retention_days=retention_days,
                min_keep_days=min_keep_days,
                max_total_bytes=1024 * GIB,
                evict_free_bytes=400 * GIB,
                min_free_bytes=250 * GIB,
                stale_tmp_seconds=2 * 3600,
                active_ticket_ids=active_ticket_ids,
                store=self.store,
            )

    def test_retention_deletes_expired_and_records_event(self) -> None:
        old = _publish_dir(self.root, "Q2026110100001", age_days=4, size=3 * GIB, now=self.now)
        fresh = _publish_dir(self.root, "Q2026111200002", age_days=0.2, size=1 * GIB, now=self.now)
        for ticket, size, age in (("Q2026110100001", 3 * GIB, 4), ("Q2026111200002", 1 * GIB, 0.2)):
            self.store.upsert_artifact(ticket, size, 1, True, (self.now - timedelta(days=age)).timestamp())

        report = self._cycle()

        self.assertEqual(report.retention_removed, 1)
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists())
        events = self.store.list_cleanup_events(limit=20)
        retention = [e for e in events if e["kind"] == CLEANUP_KIND_RETENTION]
        self.assertEqual(len(retention), 1)
        self.assertEqual(retention[0]["ticket_id"], "Q2026110100001")
        self.assertEqual(retention[0]["bytes"], 3 * GIB)
        # 流水里必须记下"当时盘还剩多少"，否则事后无法判断是不是清理不及导致的事故。
        self.assertIsNotNone(retention[0]["free_bytes"])

    def test_free_space_alone_triggers_eviction(self) -> None:
        """总量没超限，但磁盘余量低于软阈值时必须照样淘汰。

        这是本次改造的核心：已用量是代理指标，磁盘剩余是唯一真值。
        """
        victim = _publish_dir(self.root, "Q2026111000003", age_days=2, size=200 * GIB, now=self.now)
        self.store.upsert_artifact(
            "Q2026111000003", 200 * GIB, 1, True, (self.now - timedelta(days=2)).timestamp()
        )

        # 余量 300 GiB：低于软阈值 400、高于硬阈值 250 → 应当淘汰而不是熔断。
        report = self._cycle(free=300 * GIB)

        self.assertEqual(report.capacity_removed, 1)
        self.assertFalse(victim.exists())
        kinds = {e["kind"] for e in self.store.list_cleanup_events(limit=20)}
        self.assertIn(CLEANUP_KIND_CAPACITY, kinds)

    def test_no_eviction_when_both_conditions_are_healthy(self) -> None:
        keep = _publish_dir(self.root, "Q2026111100004", age_days=2, size=10 * GIB, now=self.now)
        self.store.upsert_artifact(
            "Q2026111100004", 10 * GIB, 1, True, (self.now - timedelta(days=2)).timestamp()
        )

        report = self._cycle(free=900 * GIB)

        self.assertFalse(report.touched)
        self.assertTrue(keep.exists())

    def test_min_keep_days_protects_fresh_artifacts(self) -> None:
        """刚同步完的产物即使余量告急也不删：宁可失败也不误删正在用的数据。"""
        fresh = _publish_dir(self.root, "Q2026111200005", age_days=0.1, size=200 * GIB, now=self.now)
        self.store.upsert_artifact(
            "Q2026111200005", 200 * GIB, 1, True, (self.now - timedelta(hours=2)).timestamp()
        )

        report = self._cycle(free=100 * GIB)

        self.assertEqual(report.capacity_removed, 0)
        self.assertTrue(fresh.exists())
        self.assertTrue(report.below_waterline_after)

    def test_orphan_rows_are_reconciled_every_cycle(self) -> None:
        """目录被程序外删除后，记账行必须在同一轮里清掉，否则会多删真实产物。

        2026-11-12 现场人工 rm 了 41 个目录、577 GiB，程序完全不知道，
        DB 总数虚高到 1467 GiB 而磁盘只有 889 GiB。
        """
        ghost = self.root / "Q2026110600006"
        self.store.upsert_artifact("Q2026110600006", 500 * GIB, 1, True, self.now.timestamp())
        self.assertFalse(ghost.exists())

        missing = reconcile_missing(self.root, self.store, frozenset())

        self.assertEqual(missing, [("Q2026110600006", 500 * GIB)])
        self.assertEqual(self.store.artifact_total(), 0)

        # 再出现一条幽灵行，交给整轮清理处理：这一轮必须既清行又落流水。
        self.store.upsert_artifact("Q2026110700012", 300 * GIB, 1, True, self.now.timestamp())
        report = self._cycle()
        self.assertEqual(report.orphan_rows, 1)
        self.assertEqual(report.orphan_bytes, 300 * GIB)
        kinds = {e["kind"] for e in self.store.list_cleanup_events(limit=20)}
        self.assertIn(CLEANUP_KIND_RECONCILE, kinds)

    def test_trash_is_purged_not_kept(self) -> None:
        """回收站不再保留旧版本：原件在日志源服务器上可随时重拉。"""
        leftover = self.trash / "Q2026110100007-20261110-120000"
        leftover.mkdir()
        (leftover / "big.log").write_text("x", encoding="utf-8")

        report = self._cycle()

        self.assertEqual(report.trash_purged, 1)
        self.assertFalse(leftover.exists())

    def test_active_workspace_is_not_purged_even_if_stale(self) -> None:
        """正在同步的 `.tmp` 目录不能按年龄回收：它可能正在续传或解压。

        回收判据是 mtime，而 rsync 卡在远端时 mtime 不会刷新。若不排除活跃工单，
        清理会把正在使用的 65 GiB 半成品删掉，续传与当前合并树一起作废。
        """
        stale = self.tmp_root / "Q2026111200011"
        stale.mkdir()
        (stale / "archive.zip").write_bytes(b"x" * 1024)
        old = (self.now - timedelta(hours=5)).timestamp()
        os.utime(stale, (old, old))

        report = self._cycle(
            active_ticket_ids=frozenset({"Q2026111200011"}),
        )
        self.assertEqual(report.stale_tmp_removed, 0)
        self.assertTrue(stale.exists())

        # 不在活跃集合里时照常回收，避免"保护"变成永不回收的泄漏。
        report = self._cycle()
        self.assertEqual(report.stale_tmp_removed, 1)
        self.assertFalse(stale.exists())

    def test_stale_tmp_released_before_evicting_artifacts(self) -> None:
        """盘压时先蒸发可再生的 `.tmp`，够用就不该删用户还要用的已发布产物。

        这一步的顺序是本地分支的核心诉求：旧的每日检查只按总量淘汰，
        `.tmp` 里几十 GiB 的解压残留不被计入，结果日志工单先被删掉。
        """
        victim = _publish_dir(self.root, "Q2026111000012", age_days=2, size=200 * GIB, now=self.now)
        self.store.upsert_artifact(
            "Q2026111000012", 200 * GIB, 1, True, (self.now - timedelta(days=2)).timestamp()
        )
        stale = self.tmp_root / "Q2026110900013"
        stale.mkdir()
        (stale / "archive.zip").write_bytes(b"x" * 1024)
        old = (self.now - timedelta(hours=5)).timestamp()
        os.utime(stale, (old, old))

        def fake_disk_usage(_root):
            # 回收 stale .tmp 之前余量 300 GiB（低于软阈值 400），之后回到 500 GiB。
            free = 300 * GIB if stale.exists() else 500 * GIB
            return SimpleNamespace(free=free, total=2048 * GIB, used=0)

        with patch("app.maintenance.shutil.disk_usage", side_effect=fake_disk_usage):
            report = run_cleanup_cycle(
                root=self.root,
                tmp_root=self.tmp_root,
                trash_dir=self.trash,
                now=self.now,
                retention_days=3,
                min_keep_days=1,
                max_total_bytes=1024 * GIB,
                evict_free_bytes=400 * GIB,
                min_free_bytes=250 * GIB,
                stale_tmp_seconds=2 * 3600,
                active_ticket_ids=frozenset(),
                store=self.store,
            )

        self.assertEqual(report.stale_tmp_removed, 1)
        self.assertFalse(stale.exists())
        self.assertEqual(report.capacity_removed, 0)
        self.assertTrue(victim.exists())

    def test_cutoff_uses_completion_time_not_ticket_date(self) -> None:
        """保留期判据是同步完成时刻，不是工单号里的日期。

        客户可能把历史工单隔几天再上传：按工单号日期算会把它判成早已过期。
        """
        # 工单号是 11-01 的，但今天才同步完成 → 必须保留。
        late_upload = _publish_dir(
            self.root, "Q2026110100008", age_days=1.5, size=1 * GIB, now=self.now
        )
        self.store.upsert_artifact(
            "Q2026110100008", 1 * GIB, 1, True, (self.now - timedelta(days=1.5)).timestamp()
        )

        self._cycle()

        self.assertTrue(late_upload.exists())


class PublishTest(unittest.TestCase):
    def test_replaces_old_version_without_trash(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            base = Path(raw)
            work = base / ".tmp" / "Q2026111200009"
            work.mkdir(parents=True)
            (work / "new.log").write_text("new", encoding="utf-8")
            final = base / "Q2026111200009"
            final.mkdir()
            (final / "old.log").write_text("old", encoding="utf-8")

            replaced = publish(work, final)

            self.assertEqual(replaced, final)
            self.assertTrue((final / "new.log").exists())
            self.assertFalse((final / "old.log").exists())
            self.assertFalse((base / ".trash").exists())


class CleanedListingTest(unittest.IsolatedAsyncioTestCase):
    """删除后的工单必须以「已清理」留在列表里，不能凭空消失。"""

    async def test_cleaned_ticket_shows_up_with_phase_cleaned(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            os.environ["TICKET_ROOT"] = raw
            os.environ["TICKET_AUTO_SYNC_ENABLED"] = "false"
            try:
                from app.main import JobRegistry

                cfg = Config.from_env()
                registry = JobRegistry(cfg)
                try:
                    # 造一条墓碑：模拟"同步过、后来被清理掉了"。
                    registry.store.add_tombstone(
                        "Q2026110500010", datetime.now(TZ).timestamp(), "cleanup"
                    )
                    items = await registry.list_artifacts()
                finally:
                    registry.close()
            finally:
                os.environ.pop("TICKET_ROOT", None)
                os.environ.pop("TICKET_AUTO_SYNC_ENABLED", None)

        entry = next(item for item in items if item["ticketId"] == "Q2026110500010")
        self.assertEqual(entry["phase"], Phase.CLEANED.value)
        self.assertFalse(entry["directoryReady"])
        self.assertIsNotNone(entry["cleanedAt"])
        self.assertEqual(entry["cleanupReason"], "cleanup")


class CleanupEventQueryTest(unittest.TestCase):
    def test_events_are_queryable_by_time(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            store = Store(Path(raw) / "state.db")
            try:
                store.add_cleanup_event("retention", 1000.0, ticket_id="Q1", bytes_=10)
                store.add_cleanup_event("capacity", 2000.0, ticket_id="Q2", bytes_=20)
                self.assertEqual(len(store.list_cleanup_events(limit=10)), 2)
                self.assertEqual(len(store.list_cleanup_events(limit=10, since=1500.0)), 1)
                totals = store.cleanup_totals(since=0.0)
                self.assertEqual(totals["retention"]["count"], 1)
                self.assertEqual(totals["capacity"]["bytes"], 20)
                self.assertEqual(store.prune_cleanup_events(before=1500.0), 1)
            finally:
                store.close()


class PartialExtractionCleanupTest(unittest.TestCase):
    def test_cleanup_expired_skips_active_ticket(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            now = datetime(2026, 11, 12, 12, 0, tzinfo=TZ)
            active = _publish_dir(root, "Q2026110100011", age_days=5, size=1 * GIB, now=now)

            removed = cleanup_expired(root, now, 3, {"Q2026110100011"})

            self.assertEqual(removed, 0)
            self.assertTrue(active.exists())


if __name__ == "__main__":
    unittest.main()
