"""多次采集包合并的纯函数测试（2026-11-12）。

产品规则只有两条，测试必须逐条锁住：
1. 同相对路径冲突时**最新包赢**（mtime 大者；同 mtime 规范名优先）；
2. 不冲突的文件全部保留，最终目录里同一路径只出现一次。
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from app.fetcher import RemoteArchive
from app.maintenance import write_sync_meta
from app.merge import (
    archive_key,
    baseline_keys,
    build_baseline_records,
    build_merged_records,
    clone_tree,
    max_merged_archive,
    merge_into,
    merged_keys,
    plan_pending,
    tree_stats,
)

TICKET = "Q2026111200001"


def _archive(mtime: float, *, canonical: bool = True, size: int = 100) -> RemoteArchive:
    name = f"{TICKET}.zip" if canonical else f"{TICKET}-20261112120000.zip"
    return RemoteArchive(TICKET, f"/sf/log/{TICKET}/{name}", size, mtime)


def _write(root: Path, relative: str, content: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


class CloneTreeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="ticket-sync-merge-clone-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_files_are_hardlinked_not_copied(self) -> None:
        """65 GiB 级产物不能真的复制一遍；硬链接必须逐个文件成立。"""
        src = self.root / "final"
        _write(src, "sp_10.0.0.1/logs/a.log", "hello")
        dst = self.root / "build"

        clone_tree(src, dst)

        source_info = (src / "sp_10.0.0.1/logs/a.log").stat()
        target_info = (dst / "sp_10.0.0.1/logs/a.log").stat()
        self.assertEqual(source_info.st_ino, target_info.st_ino)
        self.assertEqual((dst / "sp_10.0.0.1/logs/a.log").read_text(encoding="utf-8"), "hello")

    def test_symlink_is_recreated_without_following(self) -> None:
        src = self.root / "final"
        _write(src, "target.txt", "x")
        os.symlink("target.txt", src / "link.txt")
        dst = self.root / "build"

        clone_tree(src, dst)

        self.assertTrue((dst / "link.txt").is_symlink())
        self.assertEqual(os.readlink(dst / "link.txt"), "target.txt")


class MergeIntoTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="ticket-sync-merge-into-"))
        self.build = self.root / "build"
        self.build.mkdir()

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _package(self, name: str, files: dict[str, str]) -> Path:
        package = self.root / name
        for relative, content in files.items():
            _write(package, relative, content)
        return package

    def test_newer_package_overwrites_conflict_and_keeps_others(self) -> None:
        _write(self.build, "same.txt", "old")
        _write(self.build, "only-old.txt", "keep")
        package = self._package(
            "new",
            {"same.txt": "new", "only-new.txt": "added"},
        )

        stats = merge_into(package, self.build, overwrite=True)

        self.assertEqual((self.build / "same.txt").read_text(encoding="utf-8"), "new")
        self.assertEqual((self.build / "only-old.txt").read_text(encoding="utf-8"), "keep")
        self.assertEqual((self.build / "only-new.txt").read_text(encoding="utf-8"), "added")
        self.assertEqual(stats.overwritten, 1)
        self.assertEqual(stats.added, 1)

    def test_older_package_only_fills_missing_paths(self) -> None:
        """乱序旧包不能把已合并的新内容覆盖回去。"""
        _write(self.build, "same.txt", "newer")
        package = self._package(
            "older",
            {"same.txt": "older", "recovered.txt": "only-in-old"},
        )

        stats = merge_into(package, self.build, overwrite=False)

        self.assertEqual((self.build / "same.txt").read_text(encoding="utf-8"), "newer")
        self.assertEqual((self.build / "recovered.txt").read_text(encoding="utf-8"), "only-in-old")
        self.assertEqual(stats.skipped_conflicts, 1)

    def test_file_and_directory_conflicts_follow_newest(self) -> None:
        """旧包在 a 是目录、新包在 a 是文件时，新包必须整体胜出。"""
        _write(self.build, "a/old.txt", "x")
        package = self._package("new", {"a": "now-a-file"})

        merge_into(package, self.build, overwrite=True)

        self.assertTrue((self.build / "a").is_file())
        self.assertEqual((self.build / "a").read_text(encoding="utf-8"), "now-a-file")

    def test_empty_source_is_noop(self) -> None:
        package = self.root / "empty"
        package.mkdir()

        stats = merge_into(package, self.build, overwrite=True)

        self.assertEqual((stats.added, stats.overwritten, stats.skipped_conflicts), (0, 0, 0))


class CloneThenMergeSafetyTest(unittest.TestCase):
    """克隆出来的树与旧产物共享 inode：合并只能换目录项，绝不能原地改内容。"""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="ticket-sync-merge-inode-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_overwrite_does_not_touch_published_artifact(self) -> None:
        final = self.root / "final"
        _write(final, "shared.log", "old")
        build = self.root / "build"
        clone_tree(final, build)
        package = self.root / "new-package"
        _write(package, "shared.log", "new")

        merge_into(package, build, overwrite=True)

        self.assertEqual((build / "shared.log").read_text(encoding="utf-8"), "new")
        # 旧产物（随后会被移入 .trash 供回退）必须仍是旧内容
        self.assertEqual((final / "shared.log").read_text(encoding="utf-8"), "old")


class TreeStatsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="ticket-sync-merge-stats-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_meta_is_not_counted_as_content(self) -> None:
        _write(self.root, "a.log", "12345")
        write_sync_meta(self.root, bytes=5, files=1)

        stats = tree_stats(self.root, exclude_names={".sync-meta.json"})

        self.assertEqual(stats.bytes, 5)
        self.assertEqual(stats.files, 1)

    def test_symlink_is_not_counted_or_followed(self) -> None:
        _write(self.root, "a.log", "12345")
        os.symlink("a.log", self.root / "link.log")

        stats = tree_stats(self.root)

        self.assertEqual(stats.files, 1)
        self.assertEqual(stats.bytes, 5)


class PlanPendingTest(unittest.TestCase):
    def _meta(self, **fields) -> dict:
        return {"completedAt": 1.0, "bytes": 1, "files": 1, **fields}

    def test_first_sync_merges_every_archive(self) -> None:
        archives = [_archive(100), _archive(200), _archive(300)]

        pending = plan_pending(TICKET, archives, {}, "auto", has_final=False)

        self.assertEqual([item.mtime for item in pending], [100, 200, 300])

    def test_v2_auto_skips_merged_and_baseline(self) -> None:
        meta = self._meta(
            mergedArchives=[{"mtime": 100.0, "size": 100, "path": "a"}],
            baselineArchives=[{"mtime": 200.0, "size": 100, "path": "b"}],
        )

        pending = plan_pending(
            TICKET, [_archive(100), _archive(200), _archive(300)], meta, "auto", has_final=True
        )

        self.assertEqual([item.mtime for item in pending], [300])

    def test_v2_manual_also_backfills_baseline(self) -> None:
        meta = self._meta(
            mergedArchives=[{"mtime": 100.0, "size": 100, "path": "a"}],
            baselineArchives=[{"mtime": 200.0, "size": 100, "path": "b"}],
        )

        pending = plan_pending(
            TICKET, [_archive(100), _archive(200), _archive(300)], meta, "manual", has_final=True
        )

        self.assertEqual([item.mtime for item in pending], [200, 300])

    def test_legacy_auto_only_merges_newer_than_recorded(self) -> None:
        """升级首轮绝不能把历史包全部重拉（2026-11-01 的 766 GiB 教训）。"""
        meta = self._meta(remoteMtime=200.0, remoteSize=100)

        pending = plan_pending(
            TICKET, [_archive(100), _archive(200), _archive(300)], meta, "auto", has_final=True
        )

        self.assertEqual([item.mtime for item in pending], [300])

    def test_legacy_auto_without_identity_falls_back_to_latest(self) -> None:
        pending = plan_pending(TICKET, [_archive(100), _archive(200)], {}, "auto", has_final=True)

        self.assertEqual([item.mtime for item in pending], [200])

    def test_legacy_auto_can_backfill_when_operator_opts_in(self) -> None:
        """存量补齐靠开关显式打开，而不是把代价偷偷摊到默认巡检里。"""
        meta = self._meta(remoteMtime=200.0, remoteSize=100)

        pending = plan_pending(
            TICKET,
            [_archive(100), _archive(200)],
            meta,
            "auto",
            has_final=True,
            backfill_legacy=True,
        )

        self.assertEqual([item.mtime for item in pending], [100, 200])

    def test_legacy_manual_backfills_everything(self) -> None:
        meta = self._meta(remoteMtime=200.0, remoteSize=100)

        pending = plan_pending(TICKET, [_archive(100), _archive(200)], meta, "manual", has_final=True)

        self.assertEqual([item.mtime for item in pending], [100, 200])

    def test_pending_is_sorted_old_to_new_with_canonical_tiebreak(self) -> None:
        stamped = _archive(200, canonical=False)
        canonical = _archive(200, canonical=True)

        pending = plan_pending(TICKET, [canonical, stamped], {}, "auto", has_final=False)

        self.assertEqual(pending[0].path.endswith("20261112120000.zip"), True)
        self.assertEqual(pending[-1].path.endswith(f"{TICKET}.zip"), True)


class MetaRecordTest(unittest.TestCase):
    def _meta(self, **fields) -> dict:
        return {"completedAt": 1.0, "bytes": 1, "files": 1, **fields}

    def test_merged_records_union_and_keep_old(self) -> None:
        old = _archive(100)
        new = _archive(200)
        meta = self._meta(mergedArchives=[{"mtime": 100.0, "size": 100, "path": old.path}])

        records = build_merged_records(meta, [new])

        self.assertEqual([record["mtime"] for record in records], [100.0, 200.0])

    def test_ignored_dump_is_recorded_so_it_is_not_probed_again(self) -> None:
        dump = _archive(300)

        records = build_merged_records({}, [], [dump])

        self.assertEqual([record["mtime"] for record in records], [300.0])

    def test_legacy_auto_records_unmerged_archives_as_baseline(self) -> None:
        archives = [_archive(100), _archive(200), _archive(300)]
        meta = self._meta(remoteMtime=200.0, remoteSize=100)

        baseline = build_baseline_records(
            archives, meta, "auto", {archive_key(_archive(300))}, has_final=True
        )

        self.assertEqual([record["mtime"] for record in baseline], [100.0, 200.0])
        self.assertEqual(baseline_keys({"baselineArchives": baseline}), {(100.0, 100), (200.0, 100)})

    def test_manual_clears_baseline(self) -> None:
        archives = [_archive(100), _archive(200)]
        meta = self._meta(
            mergedArchives=[],
            baselineArchives=[{"mtime": 100.0, "size": 100, "path": "a"}],
        )

        baseline = build_baseline_records(archives, meta, "manual", set(), has_final=True)

        self.assertEqual(baseline, [])

    def test_max_merged_archive_uses_mtime_and_canonical(self) -> None:
        meta = self._meta(
            mergedArchives=[
                {"mtime": 100.0, "size": 100, "path": f"/sf/log/{TICKET}/{TICKET}-20261112120000.zip"},
                {"mtime": 200.0, "size": 100, "path": f"/sf/log/{TICKET}/{TICKET}.zip"},
            ]
        )

        best = max_merged_archive(TICKET, meta)

        self.assertIsNotNone(best)
        self.assertEqual(best.mtime, 200.0)


class MergedKeysTest(unittest.TestCase):
    def test_missing_field_is_legacy_not_empty_set(self) -> None:
        """空集合与"字段缺失"必须可区分，否则升级首轮会全量重拉。"""
        self.assertIsNone(merged_keys({}))
        self.assertEqual(merged_keys({"mergedArchives": []}), set())

    def test_invalid_records_are_ignored(self) -> None:
        meta = {
            "mergedArchives": [
                {"mtime": True, "size": 1},
                {"mtime": 1.0, "size": "x"},
                {"mtime": 1.0, "size": 2},
            ]
        }

        self.assertEqual(merged_keys(meta), {(1.0, 2)})


if __name__ == "__main__":
    unittest.main()
