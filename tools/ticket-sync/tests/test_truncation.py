"""截断归档的降级测试：能救的日志不能整单判死。

生产背景（2026-11-02）：Q2026102802029 的内层 tgz 在 1 124 073 472 字节处被截断
（正好 1 GiB + 48 MiB 的边界），gzip 尾部 CRC+ISIZE 缺失。zip 结构合法、`unzip -t`
报 "No errors detected"、两端 md5 完全一致——任何包级校验都发现不了，只有解压到最后
才会 `tar: Unexpected EOF`。

旧实现把它当整单失败：一次失败、重试 6 次、最终 failed。而实际上报错前已经解出
3 GB 可用日志（`sf/log/**/*.log` 全在）。这与"层数超限只停不判死"是同一个哲学：
**护栏命中时应保留可用部分，而不是丢弃全部**。
"""

from __future__ import annotations

import gzip
import io
import shutil
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

from app import extractor
from app.config import Config
from app.models import ErrorCode, TicketError

TICKET_ID = "Q2026110200001"


def _make_config(root: Path, max_depth: int = 8) -> Config:
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
        max_depth=max_depth,
        concurrency=1,
        auto_sync_enabled=False,
        auto_sync_interval_seconds=30,
        retention_days=7,
    )


def _tar_gz(entries: dict[str, bytes]) -> bytes:
    """构造 tar.gz 的字节内容。"""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w:gz") as tf:
        for name, data in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return raw.getvalue()


def _truncate(payload: bytes, drop: int = 8) -> bytes:
    """砍掉 gzip 尾部（CRC32 + ISIZE），复刻生产上的截断形态。"""
    return payload[:-drop]


def _zip_with_bad_member() -> bytes:
    """构造一个"成员数据被写坏"的 zip：中央目录完好，但某成员内容被篡改。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        zf.writestr("ok.log", b"a" * 512)
        zf.writestr("bad.log", b"b" * 512)
    data = bytearray(buf.getvalue())
    offset = data.find(b"b" * 512)
    data[offset + 100] ^= 0xFF
    return bytes(data)


class TruncatedTarTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="ticket-sync-trunc-"))
        self.cfg = _make_config(self.root)

    async def asyncTearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    async def test_truncated_tar_raises_degradable_corrupt(self) -> None:
        """截断必须归 archive-corrupt（数据事实），并允许调用方降级。"""
        archive = self.root / "node.tgz"
        archive.write_bytes(_truncate(_tar_gz({"kubelet.log": b"x" * 4096})))
        target = self.root / "out"
        target.mkdir()

        with self.assertRaises(TicketError) as ctx:
            await extractor._extract_any(archive, target, "test-password")

        self.assertEqual(ctx.exception.code, ErrorCode.ARCHIVE_CORRUPT)
        self.assertTrue(ctx.exception.degradable)
        # 报错前的内容已经落地，这正是"可降级"的依据。
        self.assertTrue((target / "kubelet.log").is_file())

    async def test_nested_truncated_archive_keeps_extracted_content(self) -> None:
        """工件包内的节点 tgz 截断：任务成功、日志保留、计数可查。"""
        work_dir = self.root / ".tmp" / TICKET_ID
        work_dir.mkdir(parents=True)
        archive = work_dir / f"{TICKET_ID}.zip"
        truncated = _truncate(_tar_gz({"sf/log/kubelet.log": b"log-line\n" * 512}))
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("log_list.json", "{}")
            zf.writestr("10.0.0.1-20261102-1000.tgz", truncated)

        stats = await extractor.extract(
            TICKET_ID, archive, work_dir, self.cfg, "job-trunc"
        )

        self.assertEqual(stats.truncated_archives, 1)
        self.assertEqual(stats.unreadable_archives, 0)
        # 截断前解出的内容必须保留
        self.assertTrue((work_dir / "10.0.0.1-20261102-1000" / "sf/log/kubelet.log").is_file())
        # 残缺压缩包要删掉，否则下一轮会被反复扫到、反复失败
        self.assertFalse((work_dir / "10.0.0.1-20261102-1000.tgz").exists())

    async def test_top_level_truncation_still_publishes(self) -> None:
        """顶层归档截断也能降级：解出的部分比整单 failed 有用。"""
        work_dir = self.root / ".tmp" / TICKET_ID
        work_dir.mkdir(parents=True)
        archive = work_dir / f"{TICKET_ID}.tar.gz"
        archive.write_bytes(_truncate(_tar_gz({"sf/log/tgtd.log": b"line\n" * 2048})))

        stats = await extractor.extract(
            TICKET_ID, archive, work_dir, self.cfg, "job-top-trunc"
        )

        self.assertEqual(stats.truncated_archives, 1)
        self.assertTrue((work_dir / "sf/log/tgtd.log").is_file())

    async def test_truncation_without_any_output_still_fails(self) -> None:
        """一点都没解出来就不该"降级成功"——那会发布一个空目录。"""
        work_dir = self.root / ".tmp" / TICKET_ID
        work_dir.mkdir(parents=True)
        archive = work_dir / f"{TICKET_ID}.tar.gz"
        archive.write_bytes(_truncate(_tar_gz({"a.log": b"x" * 1024}), drop=200))

        with self.assertRaises(TicketError):
            await extractor.extract(
                TICKET_ID, archive, work_dir, self.cfg, "job-empty-trunc"
            )

    async def test_corrupt_member_is_degraded(self) -> None:
        """成员级坏数据（unzip exit=80 / bad CRC）同样降级。

        实测 Q2026102802029 除了 tgz 截断，还有 `LOG_tcpstat.txt.zip` 成员数据损坏：
        `unzip exit=80`，坏的是单个成员，而当时已经解出 26 GB 内容。
        """
        work_dir = self.root / ".tmp" / TICKET_ID
        work_dir.mkdir(parents=True)
        archive = work_dir / f"{TICKET_ID}.zip"
        archive.write_bytes(_zip_with_bad_member())

        stats = await extractor.extract(TICKET_ID, archive, work_dir, self.cfg, "job-bad-member")

        self.assertEqual(stats.truncated_archives, 1)
        self.assertTrue((work_dir / "ok.log").is_file())

    async def test_clean_archive_reports_no_truncation(self) -> None:
        work_dir = self.root / ".tmp" / TICKET_ID
        work_dir.mkdir(parents=True)
        archive = work_dir / f"{TICKET_ID}.tar.gz"
        archive.write_bytes(_tar_gz({"sf/log/ok.log": b"ok\n"}))

        stats = await extractor.extract(TICKET_ID, archive, work_dir, self.cfg, "job-ok")

        self.assertEqual(stats.truncated_archives, 0)


if __name__ == "__main__":
    unittest.main()
