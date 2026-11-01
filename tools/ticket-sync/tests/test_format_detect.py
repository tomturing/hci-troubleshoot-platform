"""归档格式判定测试：扩展名不可信，必须看魔数。

生产背景（2026-11-01）：Q2026102700681 / Q2026102800262 / Q2026102801855 三个工单的
`<工单号>.zip` 实际是**未压缩 tar**（offset 257 是 `ustar`，内容为 IP-时间.tgz）。
旧实现按扩展名调用 `unzip`，必然 exit=9，被归类为 archive-corrupt 后进入永不成功的
重试循环：3 个包、12.2 GiB 磁盘和带宽被反复消耗，且没有任何重试次数能救回来。
"""

from __future__ import annotations

import gzip
import shutil
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

from app import extractor
from app.config import Config
from app.models import ErrorCode, TicketError

TICKET_ID = "Q2026010100001"


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


class DetectArchiveFormatTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="ticket-sync-format-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _write(self, name: str, data: bytes) -> Path:
        path = self.root / name
        path.write_bytes(data)
        return path

    def test_zip_is_detected_by_magic(self) -> None:
        path = self.root / f"{TICKET_ID}.zip"
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("log_list.json", "{}")

        self.assertEqual(extractor.detect_archive_format(path), ("zip", "magic"))

    def test_plain_tar_disguised_as_zip_is_detected_by_magic(self) -> None:
        """事故本体：`.zip` 里装的是 tar。"""
        path = self.root / f"{TICKET_ID}.zip"
        with tarfile.open(path, "w") as tf:
            payload = b'{"logs": []}'
            info = tarfile.TarInfo("10.137.66.5-20261028-1129.tgz")
            info.size = len(payload)
            tf.addfile(info, _BytesReader(payload))

        self.assertEqual(extractor.detect_archive_format(path), ("tar", "magic"))

    def test_gzip_is_detected_as_tar_gz(self) -> None:
        path = self._write("payload.zip", gzip.compress(b"anything"))

        self.assertEqual(extractor.detect_archive_format(path)[0], "tar.gz")

    def test_rar_and_7z_magic(self) -> None:
        rar = self._write("a.bin", b"Rar!\x1a\x07\x01\x00" + b"\x00" * 32)
        seven = self._write("b.bin", b"7z\xbc\xaf\x27\x1c" + b"\x00" * 32)

        self.assertEqual(extractor.detect_archive_format(rar), ("rar", "magic"))
        self.assertEqual(extractor.detect_archive_format(seven), ("7z", "magic"))

    def test_unknown_magic_falls_back_to_suffix(self) -> None:
        path = self._write("mystery.zip", b"not an archive at all")

        self.assertEqual(extractor.detect_archive_format(path), ("zip", "suffix"))

    def test_missing_file_falls_back_to_suffix(self) -> None:
        self.assertEqual(
            extractor.detect_archive_format(self.root / "ghost.tar.gz"),
            ("tar.gz", "suffix"),
        )


class _BytesReader:
    """tarfile.addfile 需要 file-like 对象，这里避免引入 io 依赖。"""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._offset = 0

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            size = len(self._data) - self._offset
        chunk = self._data[self._offset : self._offset + size]
        self._offset += len(chunk)
        return chunk


class TarDisguisedAsZipExtractTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="ticket-sync-format-extract-"))
        self.work_dir = self.root / ".tmp" / TICKET_ID
        self.work_dir.mkdir(parents=True)
        self.cfg = _make_config(self.root)
        self.archive = self.work_dir / f"{TICKET_ID}.zip"
        with tarfile.open(self.archive, "w") as tf:
            payload = b'{"logs": ["kubelet.log"]}'
            info = tarfile.TarInfo("log_list.json")
            info.size = len(payload)
            tf.addfile(info, _BytesReader(payload))

    async def asyncTearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    async def test_tar_disguised_as_zip_is_extracted(self) -> None:
        stats = await extractor.extract(
            TICKET_ID, self.archive, self.work_dir, self.cfg, "job-format"
        )

        self.assertTrue((self.work_dir / "log_list.json").is_file())
        # 产物统计不得把归档算进去：它是输入缓存，不是交付内容。
        self.assertEqual(stats.files, 1)
        self.assertLess(stats.bytes, self.archive.stat().st_size + 1024)

    async def test_top_level_archive_is_kept_for_resume(self) -> None:
        """解压成功后归档仍留在 work_dir：由调用方在发布前释放。"""
        await extractor.extract(TICKET_ID, self.archive, self.work_dir, self.cfg, "job-keep")

        self.assertTrue(self.archive.is_file())

    async def test_top_level_archive_is_not_treated_as_nested(self) -> None:
        """暂存归档若被当成嵌套归档，产物里会多出一层同名目录。"""
        await extractor.extract(TICKET_ID, self.archive, self.work_dir, self.cfg, "job-nested")

        self.assertFalse((self.work_dir / TICKET_ID).exists())

    async def test_unknown_format_is_reported_not_silently_skipped(self) -> None:
        bogus = self.work_dir / "bogus.zip"
        bogus.write_bytes(b"definitely not an archive")

        with self.assertRaises(TicketError) as ctx:
            await extractor.extract(TICKET_ID, bogus, self.work_dir, self.cfg, "job-bogus")

        self.assertIn(
            ctx.exception.code,
            {ErrorCode.ARCHIVE_CORRUPT, ErrorCode.EXTRACT_FAILED},
        )


if __name__ == "__main__":
    unittest.main()
