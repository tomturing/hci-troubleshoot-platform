"""同一工单多次采集的端到端合并（2026-11-12）。

产品规则：设备上的工单目录必须是**所有采集内容的并集**；同一相对路径冲突时以
最新采集包为准。这里用真实解压 + 真实发布跑通整条链路，只把 ssh/rsync 换成
本地文件拷贝，锁住四件事：

1. 首次同步把目录里全部归档都合并进来（不是只取最新一份）；
2. 第二次采集后只拉新增包，旧包内容不丢，冲突文件被新包覆盖；
3. 升级前的历史目录（meta 没有 mergedArchives）自动任务不重拉历史包，只合新增；
4. 手动同步会回填历史包（用户显式要求"要全部内容"）。
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from app.config import Config
from app.fetcher import RemoteArchive
from app.main import ORIGIN_AUTO, ORIGIN_MANUAL, JobRegistry
from app.maintenance import read_sync_meta, write_sync_meta
from app.publisher import MERGE_ARCHIVES_DIR

TICKET = "Q2026111200001"
REMOTE_ROOT = f"/sf/log/{TICKET}"


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


class FakeRemote:
    """把"远端工单目录"映射成本地 zip 文件，模拟 ssh+rsync 的拉取语义。"""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.packages: dict[str, tuple[float, bytes]] = {}
        self.fetched: list[str] = []

    def add(self, name: str, mtime: float, files: dict[str, str]) -> RemoteArchive:
        import io

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as zf:
            for relative, content in files.items():
                zf.writestr(relative, content)
        data = buffer.getvalue()
        self.packages[name] = (mtime, data)
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / name).write_bytes(data)
        return RemoteArchive(TICKET, f"{REMOTE_ROOT}/{name}", len(data), mtime)

    def archives(self) -> list[RemoteArchive]:
        return [
            RemoteArchive(TICKET, f"{REMOTE_ROOT}/{name}", len(data), mtime)
            for name, (mtime, data) in self.packages.items()
        ]

    def _sort_key(self, archive: RemoteArchive) -> tuple[float, int]:
        # 与 fetcher.archive_sort_key 同源：mtime 升序，同 mtime 规范名压后（最后合并）。
        return (archive.mtime, 1 if archive.name == f"{TICKET}.zip" else 0)

    async def list_ticket_archives(self, cfg, ticket_id: str) -> list[RemoteArchive]:
        return sorted(self.archives(), key=self._sort_key)

    async def fetch(
        self,
        ticket_id,
        cfg,
        work_dir,
        job_id,
        on_progress=None,
        remote_path=None,
        target_name=None,
    ):
        name = Path(remote_path).name
        self.fetched.append(name)
        work_dir.mkdir(parents=True, exist_ok=True)
        target = work_dir / (target_name or f"{ticket_id}.zip")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(self.packages[name][1])
        if on_progress is not None:
            on_progress(len(self.packages[name][1]), len(self.packages[name][1]))
        return target


async def _no_remote_entries(*_args, **_kwargs):
    return ["log_list.json", "sp_10.0.0.1.tgz"]


class MergeFlowTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="ticket-sync-flow-"))
        self.remote_root = self.root / "remote"
        self.cfg = _make_config(self.root / "tickets")
        self.registry = JobRegistry(self.cfg)
        self.remote = FakeRemote(self.remote_root)
        self._patches = [
            patch("app.main.list_ticket_archives", self.remote.list_ticket_archives),
            patch("app.main.fetch", self.remote.fetch),
            patch("app.main.list_remote_entries", _no_remote_entries),
        ]
        for patcher in self._patches:
            patcher.start()

    def tearDown(self) -> None:
        for patcher in self._patches:
            patcher.stop()
        self.registry.close()
        shutil.rmtree(self.root, ignore_errors=True)

    @property
    def final_dir(self) -> Path:
        return self.cfg.root / TICKET

    async def _run(self, origin: str = ORIGIN_AUTO):
        if origin == ORIGIN_MANUAL:
            job = await self.registry.create(TICKET, "req-manual")
        else:
            # 自动任务由巡检按"每工单最新一份"触发；这里手工构造同样的输入。
            archives = self.remote.archives()
            trigger = (
                sorted(archives, key=lambda item: item.mtime)[-1]
                if archives
                else RemoteArchive(TICKET, f"{REMOTE_ROOT}/{TICKET}.zip", 0, 0.0)
            )
            job = await self.registry.create_auto(trigger)
            if job is None:
                return None
        assert job.task is not None
        await job.task
        return job

    def _read(self, relative: str) -> str:
        return (self.final_dir / relative).read_text(encoding="utf-8")

    async def test_first_sync_merges_every_collected_package(self) -> None:
        """首次同步就要拿到全部采集内容，而不是最后一次。"""
        self.remote.add(f"{TICKET}-20261112100000.zip", 1000.0, {"a/only-first.log": "first"})
        self.remote.add(f"{TICKET}-20261112110000.zip", 2000.0, {"b/only-second.log": "second"})
        self.remote.add(f"{TICKET}.zip", 3000.0, {"c/only-third.log": "third"})

        job = await self._run()

        self.assertEqual(job.phase.value, "ready")
        self.assertEqual(self._read("a/only-first.log"), "first")
        self.assertEqual(self._read("b/only-second.log"), "second")
        self.assertEqual(self._read("c/only-third.log"), "third")
        self.assertEqual(len(self.remote.fetched), 3)
        meta = read_sync_meta(self.final_dir)
        self.assertEqual(len(meta["mergedArchives"]), 3)

    async def test_newest_package_wins_on_conflict(self) -> None:
        self.remote.add(f"{TICKET}-20261112100000.zip", 1000.0, {"shared.log": "old"})
        self.remote.add(f"{TICKET}.zip", 2000.0, {"shared.log": "new"})

        await self._run()

        self.assertEqual(self._read("shared.log"), "new")

    async def test_canonical_package_wins_when_mtime_ties(self) -> None:
        """同一秒重传时规范名代表最新一次采集，必须压过带戳包。"""
        self.remote.add(f"{TICKET}-20261112100000.zip", 1000.0, {"shared.log": "stamped"})
        self.remote.add(f"{TICKET}.zip", 1000.0, {"shared.log": "canonical"})

        await self._run()

        self.assertEqual(self._read("shared.log"), "canonical")

    async def test_second_collection_fetches_only_new_package(self) -> None:
        """第二次采集只拉新增包：65 GiB 级的历史包不能为了合并反复重下。"""
        self.remote.add(f"{TICKET}.zip", 1000.0, {"shared.log": "v1", "only-first.log": "first"})
        await self._run()
        self.remote.fetched.clear()

        self.remote.add(
            f"{TICKET}-20261112120000.zip",
            2000.0,
            {"shared.log": "v2", "only-second.log": "second"},
        )
        job = await self._run()

        self.assertEqual(job.phase.value, "ready")
        self.assertEqual(self.remote.fetched, [f"{TICKET}-20261112120000.zip"])
        self.assertEqual(self._read("shared.log"), "v2")
        self.assertEqual(self._read("only-first.log"), "first")
        self.assertEqual(self._read("only-second.log"), "second")

    async def test_modified_old_package_keeps_its_merged_content(self) -> None:
        """已合并的旧包即使被 5888 改名，也不该被当成新包重拉。"""
        first = self.remote.add(f"{TICKET}-20261112100000.zip", 1000.0, {"only-first.log": "x"})
        await self._run()
        self.remote.fetched.clear()

        # 服务端把旧包改名（mtime 不变），并新增一个包
        payload = self.remote.packages.pop(f"{TICKET}-20261112100000.zip")
        self.remote.packages[f"{TICKET}-20261112100000-1.zip"] = (first.mtime, payload[1])
        self.remote.add(f"{TICKET}.zip", 3000.0, {"new.log": "y"})

        job = await self._run()

        self.assertEqual(job.phase.value, "ready")
        self.assertEqual(self.remote.fetched, [f"{TICKET}.zip"])

    async def test_legacy_directory_auto_merges_only_new_packages(self) -> None:
        """升级前的目录没有 mergedArchives：自动任务只合"上次记录之后"的包。"""
        self.remote.add(f"{TICKET}-20261112100000.zip", 1000.0, {"only-old.log": "old"})
        self.remote.add(f"{TICKET}.zip", 2000.0, {"shared.log": "v1"})
        await self._run()
        self.remote.fetched.clear()

        # 把 meta 退化成升级前形态：只有远端身份，没有 mergedArchives。
        meta = read_sync_meta(self.final_dir)
        write_sync_meta(
            self.final_dir,
            remoteMtime=2000.0,
            remoteSize=meta.get("remoteSize"),
            mergedArchives=None,
            baselineArchives=None,
        )
        meta = read_sync_meta(self.final_dir)
        meta.pop("mergedArchives", None)
        meta.pop("baselineArchives", None)
        import json

        (self.final_dir / ".sync-meta.json").write_text(json.dumps(meta), encoding="utf-8")

        self.remote.add(f"{TICKET}-20261112130000.zip", 3000.0, {"new-collect.log": "new"})
        job = await self._run()

        self.assertEqual(job.phase.value, "ready")
        self.assertEqual(self.remote.fetched, [f"{TICKET}-20261112130000.zip"])
        # 历史包内容仍在（来自上一次产物），且没有被重拉
        self.assertEqual(self._read("only-old.log"), "old")
        self.assertEqual(self._read("new-collect.log"), "new")
        meta = read_sync_meta(self.final_dir)
        self.assertEqual([record["mtime"] for record in meta["baselineArchives"]], [1000.0, 2000.0])

    async def test_manual_chunk_backfills_legacy_packages(self) -> None:
        """手动同步代表用户"我要全部内容"的判断，必须回填历史包。"""
        self.remote.add(f"{TICKET}-20261112100000.zip", 1000.0, {"only-old.log": "old"})
        self.remote.add(f"{TICKET}.zip", 2000.0, {"shared.log": "v1"})
        await self._run()
        self.remote.fetched.clear()

        # 删掉本地"旧包已合并"的记录，模拟升级前的目录
        import json

        meta = read_sync_meta(self.final_dir)
        meta.pop("mergedArchives", None)
        meta.pop("baselineArchives", None)
        (self.final_dir / ".sync-meta.json").write_text(json.dumps(meta), encoding="utf-8")

        job = await self._run(origin=ORIGIN_MANUAL)

        self.assertEqual(job.phase.value, "ready")
        self.assertEqual(
            sorted(self.remote.fetched),
            sorted([f"{TICKET}-20261112100000.zip", f"{TICKET}.zip"]),
        )
        self.assertEqual(self._read("only-old.log"), "old")
        meta = read_sync_meta(self.final_dir)
        self.assertEqual(len(meta["mergedArchives"]), 2)
        self.assertEqual(meta["baselineArchives"], [])

    async def test_manual_sync_without_new_packages_keeps_artifact(self) -> None:
        """已是最新时手动点击不能重建目录（那会把用户的时间花在无意义的重拉上）。"""
        self.remote.add(f"{TICKET}.zip", 1000.0, {"a.log": "x"})
        await self._run()
        before = self.final_dir.stat().st_mtime_ns
        self.remote.fetched.clear()

        job = await self._run(origin=ORIGIN_MANUAL)

        self.assertEqual(job.phase.value, "ready")
        self.assertEqual(self.remote.fetched, [])
        self.assertIn("已是最新", job.message or "")
        self.assertTrue((self.final_dir / "a.log").exists())
        self.assertEqual(self.final_dir.stat().st_mtime_ns, before)

    async def test_no_resync_after_successful_merge(self) -> None:
        """合并成功后自动扫描不能再把同一批包重新入队（否则每 30 秒重拉一次）。"""
        self.remote.add(f"{TICKET}-20261112100000.zip", 1000.0, {"a.log": "a"})
        self.remote.add(f"{TICKET}.zip", 2000.0, {"b.log": "b"})
        await self._run()

        again = await self._run()

        self.assertIsNone(again)

    async def test_failed_package_keeps_previous_artifact(self) -> None:
        """中途失败必须保住上一次的产物：删除它等于毁掉客户已有的日志。"""
        self.remote.add(f"{TICKET}.zip", 1000.0, {"a.log": "v1"})
        await self._run()

        broken = self.remote.add(f"{TICKET}-20261112120000.zip", 2000.0, {"b.log": "v2"})
        # 让第二个包在解压阶段失败：内容不是合法 zip。
        self.remote.packages[broken.name] = (broken.mtime, b"not-a-zip-at-all")
        job = await self._run()

        self.assertEqual(job.phase.value, "failed")
        self.assertEqual(self._read("a.log"), "v1")
        self.assertFalse((self.final_dir / "b.log").exists())
        # 下载半成品保留供续传，解压中间态清理掉
        work = self.cfg.tmp_dir() / TICKET
        self.assertTrue((work / MERGE_ARCHIVES_DIR).is_dir())
        self.assertFalse((work / "build").exists())

    async def test_missing_remote_directory_fails_with_not_found(self) -> None:
        """远端目录为空时是确定失败，不能创建空产物冒充"已同步"。"""
        job = await self._run()

        self.assertEqual(job.phase.value, "failed")
        self.assertEqual(job.error_code.value, "not-found")
        self.assertFalse(self.final_dir.exists())


if __name__ == "__main__":
    unittest.main()
