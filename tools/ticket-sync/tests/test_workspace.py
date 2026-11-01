"""临时工作区的续传与回收测试（2026-11-01）。

生产背景：`work_dir` 原本带着 `job_id`，每次重试都是全新目录，
`rsync --partial --inplace` 永远找不到上次的文件——配了等于没配。
代码注释里记着真实事故：单工单被重拉 192 次、累计约 450 GiB 无效流量。

改成按工单号固定目录后，引出两个必须同时成立的约束：
1. 每次任务开始要把上次的解压产物清掉，否则新旧产物混在一起发布会是脏的；
2. 已下载的归档必须留着，否则谈不上续传；但也不能无限累积，要有按年龄的回收。
"""

from __future__ import annotations

import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from app.publisher import cleanup_stale_workspaces, prepare_workspace

TICKET_ID = "Q2026010100001"
ARCHIVE = f"{TICKET_ID}.zip"


class PrepareWorkspaceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="ticket-sync-workspace-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_keeps_archive_and_drops_extracted_products(self) -> None:
        """续传要的本钱只有归档；半套解压产物留着会污染下一次发布。"""
        work = self.root / TICKET_ID
        (work / "sp_10.0.0.1").mkdir(parents=True)
        (work / "sp_10.0.0.1" / "log.tar.gz").write_bytes(b"x")
        (work / "log_list.json").write_text("{}", encoding="utf-8")
        (work / ARCHIVE).write_bytes(b"zip")

        removed = prepare_workspace(work, ARCHIVE)

        self.assertEqual(removed, 2)
        self.assertTrue((work / ARCHIVE).exists())
        self.assertFalse((work / "sp_10.0.0.1").exists())
        self.assertFalse((work / "log_list.json").exists())

    def test_keeps_rsync_partial_temp_file(self) -> None:
        """rsync 的临时文件形态是 .<name>.XXXXXX，删了就等于放弃这次续传。"""
        work = self.root / TICKET_ID
        work.mkdir()
        (work / ARCHIVE).write_bytes(b"zip")
        partial = work / f".{ARCHIVE}.AbC123"
        partial.write_bytes(b"half")

        prepare_workspace(work, ARCHIVE)

        self.assertTrue((work / ARCHIVE).exists())
        self.assertTrue(partial.exists())

    def test_missing_workspace_is_noop(self) -> None:
        self.assertEqual(prepare_workspace(self.root / "not-exist", ARCHIVE), 0)

    def test_read_only_archive_does_not_block_cleanup(self) -> None:
        """解压产物常带 000 权限，清理必须先补权限，否则会静默失败并残留。"""
        work = self.root / TICKET_ID
        locked = work / "locked"
        locked.mkdir(parents=True)
        victim = locked / "f.bin"
        victim.write_bytes(b"x")
        victim.chmod(0o000)
        locked.chmod(0o500)
        (work / ARCHIVE).write_bytes(b"zip")

        removed = prepare_workspace(work, ARCHIVE)

        self.assertEqual(removed, 1)
        self.assertFalse(locked.exists())


class CleanupStaleWorkspacesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="ticket-sync-stale-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _make(self, name: str, age_seconds: float) -> Path:
        path = self.root / name
        path.mkdir()
        (path / f"{name}.zip").write_bytes(b"zip")
        stamp = time.time() - age_seconds
        os.utime(path, (stamp, stamp))
        return path

    def test_stale_workspace_is_reclaimed(self) -> None:
        """一个再也不会被重试的工单不能永久占着几十 GiB。"""
        stale = self._make(TICKET_ID, 3 * 3600)

        self.assertEqual(cleanup_stale_workspaces(self.root, 2 * 3600), 1)
        self.assertFalse(stale.exists())

    def test_fresh_workspace_is_kept_for_resume(self) -> None:
        """正在下载的目录 mtime 一直在刷新，绝不能被当成陈旧回收。"""
        fresh = self._make(TICKET_ID, 60)

        self.assertEqual(cleanup_stale_workspaces(self.root, 2 * 3600), 0)
        self.assertTrue(fresh.exists())

    def test_missing_tmp_root_is_noop(self) -> None:
        self.assertEqual(cleanup_stale_workspaces(self.root / "nope", 7200), 0)


if __name__ == "__main__":
    unittest.main()
