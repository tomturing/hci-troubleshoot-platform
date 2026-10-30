"""发布与清理测试：重点是失败路径上的 .tmp 不能被静默泄漏。

生产背景：老实现用 `shutil.rmtree(ignore_errors=True)` 收尾，解压产物里
存在 000 权限文件与缺执行位目录时删除会中途失败，但异常被吞、日志照常
打印"已清理"，实测单机累积 588 个残留目录。
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from app.publisher import cleanup_workspace


class CleanupWorkspaceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="ticket-sync-clean-"))
        self.work = self.root / ".tmp" / "Q2026010100001-deadbeef"
        self.work.mkdir(parents=True)

    def tearDown(self) -> None:
        for dirpath, dirnames, filenames in os.walk(self.root):
            for name in dirnames + filenames:
                try:
                    os.chmod(os.path.join(dirpath, name), 0o755)
                except OSError:
                    pass
        shutil.rmtree(self.root, ignore_errors=True)

    def test_removes_tree_with_unreadable_nodes(self) -> None:
        """000 文件 + 缺执行位目录必须被真正删掉，而不是留下空壳。"""
        nested = self.work / "sf" / "log" / "kubelet"
        nested.mkdir(parents=True)
        (nested / "kubelet.log").write_text("x" * 1024, encoding="utf-8")
        (self.work / ".pmxcfs.lockfile").write_text("", encoding="utf-8")

        os.chmod(self.work / ".pmxcfs.lockfile", 0o000)
        os.chmod(nested, 0o644)

        cleanup_workspace(self.work)

        self.assertFalse(self.work.exists())

    def test_missing_dir_is_noop(self) -> None:
        """重复清理不应报错（成功路径上 work_dir 已被 rename 走）。"""
        cleanup_workspace(self.root / ".tmp" / "not-exists")


if __name__ == "__main__":
    unittest.main()
