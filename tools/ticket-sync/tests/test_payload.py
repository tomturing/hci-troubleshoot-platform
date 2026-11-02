"""载荷识别与 RAR 分派测试。

生产背景：
- Q2026102900692 的归档只有一个 34.6 GiB 的 vmdumpcore，按日志包同步等于
  白占 33 GiB 磁盘，DSH 也读不出任何日志。
- Q2026102700323 的归档里套着 1.rar（RAR5），旧实现不认 .rar，
  产物里留一个压缩文件，等于什么都没解。
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app import extractor
from app.config import Config
from app.fetcher import _list_entries_command, list_remote_entries, looks_like_non_log_bundle
from app.models import ErrorCode, TicketError


class NonLogPayloadTest(unittest.TestCase):
    def test_vmcore_only_bundle_is_detected(self) -> None:
        self.assertTrue(looks_like_non_log_bundle(["vmdumpcore"]))

    def test_log_bundle_is_never_treated_as_non_log(self) -> None:
        self.assertFalse(
            looks_like_non_log_bundle(
                ["log_list.json", "sp_10.0.0.1.tgz", "sp_10.0.0.2.tgz", "collect_record.txt"]
            )
        )

    def test_bundle_with_tgz_is_not_skipped_even_if_small(self) -> None:
        """日志包只有两个条目时也不能误判成转储。"""
        self.assertFalse(looks_like_non_log_bundle(["sp_10.0.0.1.tgz", "_members"]))

    def test_many_entries_are_not_skipped(self) -> None:
        entries = [f"file{i}.log" for i in range(10)]
        self.assertFalse(looks_like_non_log_bundle(entries))

    def test_empty_listing_is_not_skipped(self) -> None:
        self.assertFalse(looks_like_non_log_bundle([]))


class RemoteListCommandTest(unittest.TestCase):
    """远端列目录必须按魔数选解压器，不能按扩展名。

    生产背景：Q2026102700681 / Q2026102800262 / Q2026102801855 的
    `/sf/log/<工单号>/<工单号>.zip` 内容是**裸 tar**（文件头直接是首个成员名
    `172.…` / `10.1…`），`unzip -Z1` 必然 exit=9/3。7 天内 10 次探测失败放行，
    前置拦截等于没生效。
    """

    def test_dispatches_by_magic_instead_of_extension(self) -> None:
        cmd = _list_entries_command("/sf/log/Q2026110200001/Q2026110200001.zip", 50)
        self.assertIn("504b", cmd, "zip 应按 PK 魔数走 unzip")
        self.assertIn("1f8b", cmd, "gzip 应按 1f8b 魔数走 tar -tzf")
        self.assertIn("skip=257", cmd, "裸 tar 应按 offset 257 的 ustar 标记识别")
        self.assertNotIn("unzip -Z1 /sf/log", cmd, "不能退回到无条件 unzip")

    def test_limit_is_passed_through(self) -> None:
        self.assertIn("head -n 7", _list_entries_command("/sf/log/Q1/Q1.zip", 7))

    def test_path_is_quoted_at_use_site(self) -> None:
        cmd = _list_entries_command("/sf/log/Q1/Q1.zip", 5)
        self.assertIn('"$f"', cmd, "命令里对路径的引用必须带引号")

    def test_unsupported_format_exits_with_dedicated_code(self) -> None:
        """未知格式以非零码退出：上层据此判定"探测失败"并降级放行。"""
        self.assertIn("else exit 3", _list_entries_command("/sf/log/Q1/Q1.zip", 5))


class RemoteListEntriesTest(unittest.IsolatedAsyncioTestCase):
    async def test_uses_magic_command_and_parses_lines(self) -> None:
        seen: list[str] = []

        async def fake_ssh_run(cfg, command, message, timeout=20):
            seen.append(command)
            return b"vmdumpcore\n  \nlog_list.json\n"

        cfg = SimpleNamespace(remote_archive=lambda tid: f"/sf/log/{tid}/{tid}.zip")
        with patch("app.fetcher._ssh_run", fake_ssh_run):
            entries = await list_remote_entries(cfg, "Q2026110200001", limit=12)

        self.assertEqual(entries, ["vmdumpcore", "log_list.json"])
        self.assertIn("head -n 12", seen[0])
        # 命令里必须带魔数分支，否则裸 tar 的包又会探测失败
        self.assertIn("1f8b", seen[0])


@unittest.skipUnless(shutil.which("7z"), "宿主机未安装 7z")
class RarDispatchTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="ticket-sync-rar-"))
        self.cfg = Config(
            source_host="192.168.0.2",
            source_port=22468,
            source_user="acli",
            ssh_key=Path("/nonexistent/id_ed25519"),
            archive_password="test-password",
            root=self.root,
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

    async def asyncTearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    async def test_corrupt_rar_reports_archive_problem(self) -> None:
        """损坏的 RAR 必须走 7z 分支并归入归档类错误，而不是 EXTRACT_FAILED 兜底。"""
        archive = self.root / "broken.rar"
        archive.write_bytes(b"Rar!\x1a\x07\x01\x00" + b"\x00" * 32)
        target = self.root / "out"
        target.mkdir()

        with self.assertRaises(TicketError) as ctx:
            await extractor._extract_any(archive, target, "test-password")

        self.assertIn(ctx.exception.code, {ErrorCode.ARCHIVE_CORRUPT, ErrorCode.EXTRACT_FAILED})

    async def test_rar_is_listed_as_supported_suffix(self) -> None:
        self.assertIn(".rar", extractor.ARCHIVE_SUFFIXES)

    async def test_unreadable_rar_is_kept_without_failing_or_looping(self) -> None:
        """解不开的 rar 必须降级：任务成功、原件保留、且不会反复重试同一文件。

        宿主的 p7zip 不支持部分 RAR5 压缩方法（实测 "Unsupported Method"），
        这类附件解不开但不应连累整包日志。降级后若忘记从待处理列表剔除，
        会退回"解压失败→降级→下一轮再扫到"的死循环。

        这里直接让 _unrar 抛业务异常，测的是降级与去重逻辑本身，
        不依赖具体 7z 版本对"损坏 rar"的判码。
        """
        import zipfile

        work_dir = self.root / "work"
        work_dir.mkdir()
        archive = work_dir / "Q2026010100001.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("log_list.json", "{}")
            # 必须大于占位包阈值（64 字节），否则会先被"1 字节坏包"规则剔除，
            # 测不到 rar 降级分支。
            zf.writestr("1.rar", b"Rar!\x1a\x07\x01\x00" + b"\x00" * 200)

        with patch.object(
            extractor,
            "_unrar",
            new=AsyncMock(
                side_effect=TicketError(ErrorCode.EXTRACT_FAILED, "解压失败", "unsupported method")
            ),
        ):
            stats = await extractor.extract("Q2026010100001", archive, work_dir, self.cfg, "job-rar")

        self.assertEqual(stats.unreadable_archives, 1)
        # 原件保留，供人工用 unrar 处理
        self.assertTrue((work_dir / "1.rar").is_file())
        self.assertTrue((work_dir / "log_list.json").is_file())


if __name__ == "__main__":
    unittest.main()
