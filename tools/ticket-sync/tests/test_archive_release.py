"""归档释放时机测试：归档必须留到解压成功，但不能跟着产物一起发布。

生产背景（2026-11-01）：`extractor.extract()` 在**开始解压时**就 `unlink` 顶层归档
"腾空间"，导致解压中途失败后 work_dir 里连归档都没有（实测 Q2026101600842 /
Q2026102700874 / Q2026102802029 三个目录全空），下一轮重试只能从 0 重拉 16–65 GiB。
#1408 声称的"失败保留归档供续传"因此在 extract 阶段完全失效。

正确时机：解压全部成功之后、publish 之前——早了毁掉续传，晚了会跟着 rename
进产物目录，污染用户可见的目录结构。
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.config import Config
from app.main import JobRegistry

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
        max_files=200_000,
        max_depth=8,
        concurrency=1,
        auto_sync_enabled=False,
        auto_sync_interval_seconds=30,
        retention_days=7,
    )


class ArchiveReleaseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="ticket-sync-release-"))
        self.registry = JobRegistry(_make_config(self.root))
        self.work_dir = self.root / ".tmp" / TICKET_ID
        self.work_dir.mkdir(parents=True)
        self.archive = self.work_dir / f"{TICKET_ID}.zip"
        self.archive.write_bytes(b"x" * 4096)

    def tearDown(self) -> None:
        self.registry.close()
        shutil.rmtree(self.root, ignore_errors=True)

    def test_release_removes_archive(self) -> None:
        self.registry._release_archive(self.work_dir, self.archive.name)

        self.assertFalse(self.archive.exists())

    def test_release_is_idempotent(self) -> None:
        """发布路径可能被重入（重试/取消），删两次不能报错。"""
        self.registry._release_archive(self.work_dir, self.archive.name)
        self.registry._release_archive(self.work_dir, self.archive.name)

        self.assertFalse(self.archive.exists())

    def test_archive_kept_when_disk_is_ample(self) -> None:
        """余量充足时优先保续传，不做提前删除。"""
        with patch(
            "app.main.shutil.disk_usage",
            return_value=shutil._ntuple_diskusage(total=10**12, used=0, free=10**12),
        ):
            self.registry._release_archive_for_space(self.work_dir, self.archive)

        self.assertTrue(self.archive.exists())

    def test_archive_released_early_when_disk_is_tight(self) -> None:
        """放不下两份时才牺牲续传：写满磁盘比重新下载更糟。"""
        with patch(
            "app.main.shutil.disk_usage",
            return_value=shutil._ntuple_diskusage(total=10**6, used=10**6, free=1),
        ):
            self.registry._release_archive_for_space(self.work_dir, self.archive)

        self.assertFalse(self.archive.exists())


if __name__ == "__main__":
    unittest.main()
