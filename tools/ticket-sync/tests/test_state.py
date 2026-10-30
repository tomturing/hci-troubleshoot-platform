"""任务持久化与增量对账测试（2026-10-31）。

生产背景：
- 任务状态原本只存进程内存，重启后前端看不到任何历史，排查"这个工单上次
  同步成功没"只能翻 journal。现在写进 SQLite 并区分 `interrupted` 与 `failed`。
- 容量判断原本每天遍历目录、读 meta。改成 artifacts 表 + 目录 mtime 增量对账：
  只有"自上次记账后被写过"的工单才重新读 meta。
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.config import Config
from app.main import JobRegistry
from app.maintenance import enforce_capacity, reconcile_artifacts, write_sync_meta
from app.models import Phase
from app.store import Store

TZ = timezone(timedelta(hours=8))
TICKET_ID = "Q2026010100001"


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
        max_files=500_000,
        max_depth=12,
        concurrency=1,
        auto_sync_enabled=False,
        auto_sync_interval_seconds=30,
        retention_days=7,
    )


def _make_ticket(root: Path, name: str, size: int, age_days: float, now: datetime) -> Path:
    ticket = root / name
    ticket.mkdir()
    (ticket / "log.bin").write_bytes(b"x" * size)
    stamp = (now - timedelta(days=age_days)).timestamp()
    os.utime(ticket, (stamp, stamp))
    return ticket


class StoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="ticket-sync-store-"))
        self.store = Store(self.root / ".ticket-sync.db")

    def tearDown(self) -> None:
        self.store.close()
        shutil.rmtree(self.root, ignore_errors=True)

    def test_running_job_becomes_interrupted_on_restart(self) -> None:
        """重启后遗留的 running 任务必须改判 interrupted。

        保持 running 会让前端永久显示"同步中"，而实际没有任何协程在跑；
        与 failed 也不能混为一谈——重新触发通常就能成功。
        """
        now = datetime.now(TZ)
        for idx, phase in enumerate(("queued", "fetching", "extracting", "ready", "failed")):
            self.store.upsert_job(
                ticket_id=f"T{idx}",
                job_id="j",
                phase=phase,
                accepted_at=now,
                updated_at=now,
            )

        changed = self.store.mark_interrupted()

        self.assertEqual(changed, 3)
        phases = {row["ticket_id"]: row["phase"] for row in self.store.load_jobs()}
        for idx in range(3):
            self.assertEqual(phases[f"T{idx}"], Phase.INTERRUPTED.value)
        self.assertEqual(phases["T3"], "ready")
        self.assertEqual(phases["T4"], "failed")

    def test_prune_jobs_keeps_recent(self) -> None:
        now = datetime.now(TZ)
        for idx in range(10):
            self.store.upsert_job(
                ticket_id=f"Q2026010100{idx:02d}",
                job_id="j",
                phase="ready",
                accepted_at=now,
                updated_at=now + timedelta(seconds=idx),
            )

        self.store.prune_jobs(keep=3)

        self.assertEqual(len(self.store.load_jobs()), 3)

    def test_artifact_total_and_order(self) -> None:
        self.store.upsert_artifact("Q202601010001", 100, 1, True, 1000.0, 1000.0)
        self.store.upsert_artifact("Q202601010002", 200, 1, False, 2000.0, 2000.0)

        self.assertEqual(self.store.artifact_total(), 300)
        order = [row["ticket_id"] for row in self.store.eviction_order(3000.0)]
        # 非日志载荷排前面，即使它更"新"
        self.assertEqual(order[0], "Q202601010002")


class RegistryRestartTest(unittest.IsolatedAsyncioTestCase):
    """重启后仍能查到上次的任务状态。"""

    async def asyncSetUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="ticket-sync-restart-"))
        self.cfg = _make_config(self.root)

    async def asyncTearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    async def test_history_survives_restart(self) -> None:
        first = JobRegistry(self.cfg)
        # 只验证受理与落库，不真正跑 fetch。
        first._start = lambda job, request_id: None  # type: ignore[method-assign]
        await first.create(TICKET_ID, "req-1")
        first.close()

        second = JobRegistry(self.cfg)
        try:
            job = second.get(TICKET_ID)
            self.assertIsNotNone(job)
            self.assertEqual(job.phase, Phase.INTERRUPTED)
            self.assertIn("中断", job.message or "")
            items = await second.list_artifacts()
            self.assertEqual(items[0]["ticketId"], TICKET_ID)
            self.assertIsNotNone(items[0]["job"])
        finally:
            second.close()

    async def test_live_job_takes_precedence_over_history(self) -> None:
        first = JobRegistry(self.cfg)
        first._start = lambda job, request_id: None  # type: ignore[method-assign]
        await first.create(TICKET_ID, "req-1")
        first.close()

        second = JobRegistry(self.cfg)
        try:
            second._start = lambda job, request_id: None  # type: ignore[method-assign]
            await second.create(TICKET_ID, "req-2")
            job = second.get(TICKET_ID)
            # 重新触发后应以内存里的新一轮任务为准，而不是展示上一次的 interrupted。
            self.assertEqual(job.phase, Phase.QUEUED)
        finally:
            second.close()


class IncrementalReconcileTest(unittest.TestCase):
    """增量对账：只有目录被写过才重新读 meta。"""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="ticket-sync-reconcile-"))
        self.now = datetime(2026, 10, 31, 12, 0, tzinfo=TZ)
        self.store = Store(self.root / ".ticket-sync.db")

    def tearDown(self) -> None:
        self.store.close()
        shutil.rmtree(self.root, ignore_errors=True)

    def test_first_pass_adds_then_second_pass_skips(self) -> None:
        ticket = _make_ticket(self.root, "Q2026010100001", 100, 1, self.now)
        write_sync_meta(ticket, bytes=100, hasLogs=True, completedAt=self.now.timestamp())

        self.assertEqual(reconcile_artifacts(self.root, self.store), (1, 0, 0))
        # 目录没有任何变化：不读 meta、不扫体积，直接跳过
        self.assertEqual(reconcile_artifacts(self.root, self.store), (0, 0, 0))

    def test_meta_without_has_logs_is_classified(self) -> None:
        """历史 meta 只有 bytes 时也必须补判 has_logs，否则存量拿不到优先淘汰。"""
        ticket = _make_ticket(self.root, "Q2026010100001", 100, 1, self.now)
        write_sync_meta(ticket, bytes=100)  # 故意不带 hasLogs
        stamp = (self.now - timedelta(days=1)).timestamp()
        os.utime(ticket, (stamp, stamp))

        reconcile_artifacts(self.root, self.store)

        # 目录里只有 log.bin，没有 log_list.json / sp_*，应判为"无日志"
        self.assertEqual(self.store.artifact_map()["Q2026010100001"]["has_logs"], 0)
        # 判定结果要回写 meta，否则下次还得重算
        meta = json.loads((ticket / ".sync-meta.json").read_text(encoding="utf-8"))
        self.assertIs(meta["hasLogs"], False)
        # 补写 meta 不能把目录 mtime 刷掉，否则保留期判断会失真
        self.assertAlmostEqual(ticket.stat().st_mtime, stamp, places=1)

    def test_touched_directory_is_recounted(self) -> None:
        ticket = _make_ticket(self.root, "Q2026010100001", 100, 1, self.now)
        write_sync_meta(ticket, bytes=100, hasLogs=True, completedAt=self.now.timestamp())
        reconcile_artifacts(self.root, self.store)

        later = self.now.timestamp() + 60
        os.utime(ticket, (later, later))

        self.assertEqual(reconcile_artifacts(self.root, self.store), (0, 1, 0))

    def test_vanished_directory_removes_row(self) -> None:
        ticket = _make_ticket(self.root, "Q2026010100001", 100, 1, self.now)
        write_sync_meta(ticket, bytes=100, hasLogs=True, completedAt=self.now.timestamp())
        reconcile_artifacts(self.root, self.store)

        shutil.rmtree(ticket)

        self.assertEqual(reconcile_artifacts(self.root, self.store), (0, 0, 1))
        self.assertEqual(self.store.artifact_total(), 0)

    def test_enforce_with_store_prefers_non_log_payload(self) -> None:
        ten_days_ago = (self.now - timedelta(days=10)).timestamp()
        five_days_ago = (self.now - timedelta(days=5)).timestamp()
        logs = _make_ticket(self.root, "Q2026010100001", 100, 10, self.now)
        write_sync_meta(logs, bytes=100, hasLogs=True, completedAt=ten_days_ago)
        dump = _make_ticket(self.root, "Q2026010100002", 100, 5, self.now)
        write_sync_meta(dump, bytes=100, hasLogs=False, completedAt=five_days_ago)

        removed, _ = enforce_capacity(self.root, 150, 3, self.now, frozenset(), self.store, None)

        self.assertEqual(removed, 1)
        self.assertFalse(dump.exists())
        self.assertTrue(logs.exists())
        self.assertEqual(self.store.artifact_total(), 100)


if __name__ == "__main__":
    unittest.main()
