"""解压层测试：重点覆盖采集端占位坏包不再放大为整包失败。

生产背景：HCI 采集端会在个别节点写出 1 字节的 `LOG_run_shm.txt.zip`，
unzip 对其返回 exit=9。旧实现会抛出 archive-corrupt 让整个工单失败，
而自动巡检随后每 30 秒重拉整个归档，形成永不结束的失败循环
（实测单工单 192 次、累计数百 GiB 无效流量）。
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from app import extractor
from app.config import Config
from app.models import ErrorCode, TicketError

TICKET_ID = "Q2026010100001"


def _make_config(root: Path, max_depth: int = 4) -> Config:
    """构造最小可用配置：不读远端、不写产物根以外的路径。"""
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
        # 测试环境只验证解压逻辑，磁盘红线置 0 避免受 CI 机器余量影响。
        min_free_bytes=0,
        max_files=50_000,
        max_depth=max_depth,
        concurrency=1,
        auto_sync_enabled=False,
        auto_sync_interval_seconds=30,
        retention_days=7,
    )


def _build_archive(work_dir: Path, placeholder_size: int = 1) -> Path:
    """构造一个顶层工单 zip，内含一个占位坏包和一个正常文件。

    结构对齐真实包：顶层是散文件，其中 `LOG_run_shm.txt.zip` 是 1 字节残留。
    """
    archive = work_dir / f"{TICKET_ID}.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("log_list.json", "{}")
        zf.writestr("LOG_run_shm.txt.zip", b"x" * placeholder_size)
    return archive


class PlaceholderArchiveTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="ticket-sync-extract-"))
        self.work_dir = self.root / ".tmp" / f"{TICKET_ID}-test"
        self.work_dir.mkdir(parents=True)
        self.cfg = _make_config(self.root)

    async def asyncTearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    async def test_placeholder_archive_is_skipped_not_fatal(self) -> None:
        """1 字节占位包应被剔除，任务继续成功，并计入统计。"""
        archive = _build_archive(self.work_dir)

        stats = await extractor.extract(TICKET_ID, archive, self.work_dir, self.cfg, "job-test")

        self.assertEqual(stats.placeholders_skipped, 1)
        self.assertTrue((self.work_dir / "log_list.json").is_file())
        self.assertFalse((self.work_dir / "LOG_run_shm.txt.zip").exists())

    async def test_placeholder_does_not_consume_depth_budget(self) -> None:
        """占位包不产生实际解压，不应占用层数配额。

        max_depth 取 2 时，旧实现会在第二轮把占位包当作待解归档并因
        depth 用尽而失败；现在第二轮剔除后直接收敛。
        """
        archive = _build_archive(self.work_dir)
        cfg = _make_config(self.root, max_depth=2)

        stats = await extractor.extract(TICKET_ID, archive, self.work_dir, cfg, "job-test")

        self.assertEqual(stats.depth, 1)
        self.assertEqual(stats.placeholders_skipped, 1)

    async def test_broken_archive_still_reported_without_guard(self) -> None:
        """对照组：关闭占位包守卫后，同一个包必须仍然报 archive-corrupt。

        这条测试把「旧行为」钉住，防止守卫被误删或阈值被改大到失去意义。
        """
        archive = _build_archive(self.work_dir)

        with self.assertRaises(TicketError) as ctx:
            with patch.object(extractor, "PLACEHOLDER_MAX_BYTES", 0):
                await extractor.extract(TICKET_ID, archive, self.work_dir, self.cfg, "job-test")

        self.assertEqual(ctx.exception.code, ErrorCode.ARCHIVE_CORRUPT)


class PermissionNormalizeTest(unittest.TestCase):
    """产物权限归一：源端目录缺执行位会让整棵子树对 DSH 不可读。"""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="ticket-sync-perm-"))

    def tearDown(self) -> None:
        # 测试过程会把目录改成不可进入，清理时统一放宽。
        for dirpath, dirnames, filenames in os.walk(self.root):
            for name in dirnames + filenames:
                try:
                    os.chmod(os.path.join(dirpath, name), 0o755)
                except OSError:
                    pass
        shutil.rmtree(self.root, ignore_errors=True)

    def test_directory_without_execute_bit_is_fixed(self) -> None:
        """kubelet/containerd 目录实测为 drw-r--r--，必须补回 x 才能进入。"""
        kubelet = self.root / "sf" / "log" / "kubelet"
        kubelet.mkdir(parents=True)
        log = kubelet / "kubelet.log"
        log.write_text("x", encoding="utf-8")
        # 顺序有意为之：先设文件、再设目录。目录一旦去掉 x 位，
        # 连 chmod 自己下面的文件都会 Permission denied（路径无法解析）。
        os.chmod(log, 0o444)
        os.chmod(kubelet, 0o644)

        fixed = extractor._normalize_permissions(self.root)

        self.assertGreaterEqual(fixed, 2)
        self.assertTrue(os.access(kubelet, os.R_OK | os.X_OK))
        self.assertTrue(os.access(log, os.R_OK | os.W_OK))
        self.assertEqual((kubelet / "kubelet.log").read_text(encoding="utf-8"), "x")

    def test_already_correct_tree_is_untouched(self) -> None:
        """权限已正确时不应产生无谓的 chmod（避免每层全树写盘）。"""
        (self.root / "a" / "b").mkdir(parents=True)
        (self.root / "a" / "b" / "c.log").write_text("y", encoding="utf-8")

        self.assertEqual(extractor._normalize_permissions(self.root), 0)


if __name__ == "__main__":
    unittest.main()
