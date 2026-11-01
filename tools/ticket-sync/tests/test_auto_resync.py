"""远端归档换版本后的重同步（2026-11-01）。

背景：`create_auto` 的 docstring 写着「为尚未同步**或远端已更新**的归档创建自动
任务」，实现却只判断 `final_dir.is_dir()` 就 `return None`。客户重传修复版之后，
自动同步永远拉不到新包，只能靠人工在页面重新输入工单号——注释在骗人。

修法：发布时把远端身份（`remoteMtime` / `remoteSize`）随产物写进
`.sync-meta.json`，扫描时与原值比对。选这个判据而不是"和完成时间比大小"，是因为
它幂等：同一个输入永远给出同一答案，不会因为一次元数据写失败就无限重拉。

两个必须守住的方向：
- 历史目录没有这两项记录 → 一律按「没变」处理，升级首轮绝不重拉存量；
- 远端换包同时清掉失败退避：客户重传说明上次失败的原因可能已经消失。
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from app.config import Config
from app.fetcher import RemoteArchive
from app.main import ORIGIN_MANUAL, JobRegistry, _now
from app.maintenance import write_sync_meta

TICKET_ID = "Q2026110100001"
REMOTE_MTIME = 1_760_000_000.0
REMOTE_SIZE = 4096


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


class RemoteUpdateResyncTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="ticket-sync-resync-"))
        self.cfg = _make_config(self.root)
        self.registry = JobRegistry(self.cfg)
        # 只验证入队判定，不真正发起 fetch。
        self.registry._start = lambda job, request_id: None  # type: ignore[method-assign]

    def tearDown(self) -> None:
        self.registry.close()
        shutil.rmtree(self.root, ignore_errors=True)

    def _publish(self, ticket_id: str, **meta) -> Path:
        """造一份"已发布产物"，meta 里带上远端身份（可缺省）。"""
        ticket_dir = self.root / ticket_id
        ticket_dir.mkdir(parents=True, exist_ok=True)
        (ticket_dir / "log_list.json").write_text("{}", encoding="utf-8")
        fields = {"completedAt": _now().timestamp(), **meta}
        write_sync_meta(ticket_dir, **fields)
        return ticket_dir

    def _archive(self, ticket_id: str, *, mtime: float = REMOTE_MTIME, size: int = REMOTE_SIZE):
        return RemoteArchive(ticket_id, f"/sf/log/{ticket_id}/{ticket_id}.zip", size, mtime)

    async def test_same_version_is_skipped(self) -> None:
        """远端还是同一份：不该重拉几十 GiB。"""
        self._publish(TICKET_ID, remoteMtime=REMOTE_MTIME, remoteSize=REMOTE_SIZE)

        job = await self.registry.create_auto(self._archive(TICKET_ID))

        self.assertIsNone(job)

    async def test_size_change_triggers_resync(self) -> None:
        """客户重传通常改内容；rsync -a 可能带过源端旧 mtime，所以体积也要比。"""
        self._publish(TICKET_ID, remoteMtime=REMOTE_MTIME, remoteSize=REMOTE_SIZE)

        job = await self.registry.create_auto(self._archive(TICKET_ID, size=REMOTE_SIZE + 1))

        self.assertIsNotNone(job)
        self.assertEqual(job.ticket_id, TICKET_ID)

    async def test_mtime_change_triggers_resync(self) -> None:
        self._publish(TICKET_ID, remoteMtime=REMOTE_MTIME, remoteSize=REMOTE_SIZE)

        job = await self.registry.create_auto(
            self._archive(TICKET_ID, mtime=REMOTE_MTIME + 600)
        )

        self.assertIsNotNone(job)

    async def test_legacy_artifact_without_remote_identity_is_never_resynced(self) -> None:
        """改造之前发布的目录没有远端身份，判据缺失一律按「没变」处理。

        否则升级首轮就会把 95 个历史归档全部重拉一遍——2026-11-01 已经踩过。
        """
        self._publish(TICKET_ID, completedAt=_now().timestamp(), bytes=1024, files=3)

        job = await self.registry.create_auto(self._archive(TICKET_ID, mtime=REMOTE_MTIME + 9999))

        self.assertIsNone(job)

    async def test_remote_update_clears_failure_backoff(self) -> None:
        """远端换包 = 上次失败的输入已经变了，退避必须清零再试一次。"""
        self._publish(TICKET_ID, remoteMtime=REMOTE_MTIME, remoteSize=REMOTE_SIZE)
        self.registry._note_failure(TICKET_ID)
        self.assertGreater(self.registry._retries[TICKET_ID].next_retry_at, _now())

        job = await self.registry.create_auto(self._archive(TICKET_ID, size=REMOTE_SIZE + 1))

        self.assertIsNotNone(job)
        self.assertNotIn(TICKET_ID, self.registry._retries)

    async def test_backoff_inside_window_still_blocks_downstream(self) -> None:
        """退避窗口内、且远端包没变：仍然跳过，避免每轮巡检重拉坏包。"""
        self._publish(TICKET_ID, remoteMtime=REMOTE_MTIME, remoteSize=REMOTE_SIZE)
        self.registry._note_failure(TICKET_ID)
        self.registry._retries[TICKET_ID].next_retry_at = _now() + timedelta(minutes=10)

        job = await self.registry.create_auto(self._archive(TICKET_ID))

        self.assertIsNone(job)

    async def test_manual_create_still_resyncs_same_version(self) -> None:
        """手动点击不受版本判据约束：用户可能就是想重建一次本地产物。"""
        self._publish(TICKET_ID, remoteMtime=REMOTE_MTIME, remoteSize=REMOTE_SIZE)

        job = await self.registry.create(TICKET_ID, "req-manual")

        self.assertEqual(job.ticket_id, TICKET_ID)
        self.assertEqual(job.origin, ORIGIN_MANUAL)


if __name__ == "__main__":
    unittest.main()
