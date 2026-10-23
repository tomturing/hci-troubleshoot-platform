"""解压器纯函数测试：后缀识别、逃逸链接清理、统计口径。

这些函数决定安全边界，必须覆盖；解压本身依赖真实归档，放在 S1 联调验证。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from app.extractor import _find_archives, _strip_escaped_symlinks, _target_for, _tree_stats


class TargetForTest(unittest.TestCase):
    def test_strips_tar_gz_before_gz(self) -> None:
        # .tar.gz 必须整体去掉，只去掉 .gz 会得到 raid.tar/ 的错误目录名
        self.assertEqual(_target_for(Path("cmd-output/raid.tar.gz")), Path("cmd-output/raid"))

    def test_strips_tgz(self) -> None:
        self.assertEqual(_target_for(Path("sp_10.46.16.71.tgz")), Path("sp_10.46.16.71"))

    def test_strips_zip(self) -> None:
        self.assertEqual(_target_for(Path("Q2026052801304.zip")), Path("Q2026052801304"))

    def test_keeps_path_prefix(self) -> None:
        self.assertEqual(
            _target_for(Path("a/b/c.tar.gz")),
            Path("a/b/c"),
            "父目录必须保留，否则多层归档会被平铺到一起",
        )


class FindArchivesTest(unittest.TestCase):
    def test_ignores_symlinked_directory(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            outside = root.parent / f"outside-{os.getpid()}"
            outside.mkdir(exist_ok=True)
            (outside / "evil.zip").write_bytes(b"x")
            (root / "link").symlink_to(outside, target_is_directory=True)

            found = _find_archives(root)
            self.assertEqual(found, [], "不允许通过符号链接目录发现归档")

            (outside / "evil.zip").unlink()
            outside.rmdir()

    def test_finds_nested_archive(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "sub").mkdir()
            (root / "sub" / "a.tgz").write_bytes(b"x")
            (root / "b.tar.gz").write_bytes(b"x")
            (root / "note.txt").write_bytes(b"x")

            names = sorted(path.name for path in _find_archives(root))
            self.assertEqual(names, ["a.tgz", "b.tar.gz"])


class StripEscapedSymlinkTest(unittest.TestCase):
    def test_removes_escape_and_keeps_inside(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "work"
            root.mkdir()
            (root / "inside.txt").write_text("ok", encoding="utf-8")

            # 逃逸链接：指向 root 之外
            (root / "evil").symlink_to("/etc/passwd")
            # 合法链接：指向 root 内部
            (root / "good").symlink_to(root / "inside.txt")

            removed = _strip_escaped_symlinks(root)

            self.assertEqual(removed, 1)
            self.assertFalse((root / "evil").exists() or (root / "evil").is_symlink())
            self.assertTrue((root / "good").is_symlink(), "内部链接不应被删除")

    def test_removes_dangling_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "work"
            root.mkdir()
            # 断链会被 realpath 解析到 root 之外的路径，必须清理
            (root / "dangling").symlink_to("/nonexistent/target-xyz")

            removed = _strip_escaped_symlinks(root)
            self.assertEqual(removed, 1)


class TreeStatsTest(unittest.TestCase):
    def test_counts_real_files_only(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "a.bin").write_bytes(b"12345")
            (root / "sub").mkdir()
            (root / "sub" / "b.bin").write_bytes(b"123")
            (root / "link").symlink_to(root / "a.bin")

            total, count = _tree_stats(root)
            self.assertEqual(count, 2, "符号链接不计入文件数，否则配额可被绕过")
            self.assertEqual(total, 8)


if __name__ == "__main__":
    unittest.main()
