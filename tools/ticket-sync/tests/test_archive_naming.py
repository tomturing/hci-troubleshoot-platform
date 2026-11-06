"""工单归档命名与"取最新包"的测试。

背景：5888 在 2026-10-31 ~ 11-06 之间把**重传的新包**存成
`<工单号>-<YYYYmmddHHMMSS>.zip`，规范名 `<工单号>.zip` 留在首次上传的旧包上。
现场 Q2026110600493 就是这样（规范名 11:04 采集 335M，新包 12:06 采集 545M）。
本文件锁住两条行为：解析阶段同工单只取最新，拉取阶段用显式远端路径。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.fetcher import (
    _parse_remote_archives,
    _remote_spec,
    fetch,
    resolve_archive_path,
)
from app.models import ErrorCode, TicketError

TICKET = "Q2026110600493"
CANON = f"/sf/log/{TICKET}/{TICKET}.zip"
STAMPED = f"/sf/log/{TICKET}/{TICKET}-20261106155217.zip"


class ArchiveSelectionTest(unittest.TestCase):
    """目录内多份归档时，选中最新那份。"""

    def test_picks_newest_by_mtime_not_by_name(self) -> None:
        """规范名是旧包时也要选带戳的新包。"""
        output = (
            f"{CANON}\t335882103\t1793936220.0\n"
            f"{STAMPED}\t571632020\t1793951537.0\n"
        )

        archives = _parse_remote_archives(output)

        self.assertEqual(len(archives), 1)
        self.assertEqual(archives[0].path, STAMPED)
        self.assertEqual(archives[0].size, 571632020)

    def test_tie_on_mtime_prefers_canonical_name(self) -> None:
        """同 mtime（同秒重传）时以规范名为准：这是 11-06 之后 5888 的约定。"""
        output = (
            f"{STAMPED}\t100\t1793951537.0\n"
            f"{CANON}\t200\t1793951537.0\n"
        )

        archives = _parse_remote_archives(output)

        self.assertEqual([item.path for item in archives], [CANON])

    def test_rejects_cross_directory_reference(self) -> None:
        """目录名与文件名不是同一工单号的一律丢弃，避免把别单内容同步进来。"""
        output = (
            f"/sf/log/{TICKET}/Q2026110600999.zip\t100\t1793951537.0\n"
            f"/sf/log/{TICKET}/../{TICKET}/{TICKET}.zip\t100\t1793951537.0\n"
            f"/sf/log/Q2026110600999/{TICKET}.zip\t100\t1793951537.0\n"
        )

        self.assertEqual(_parse_remote_archives(output), [])

    def test_rejects_malformed_stamp(self) -> None:
        """时间戳段必须是 14 位数字，`-latest` / `-2026` 之类不算归档。"""
        output = (
            f"/sf/log/{TICKET}/{TICKET}-latest.zip\t100\t1793951537.0\n"
            f"/sf/log/{TICKET}/{TICKET}-2026.zip\t100\t1793951537.0\n"
        )

        self.assertEqual(_parse_remote_archives(output), [])

    def test_accepts_sequence_suffix(self) -> None:
        """同秒撞车时 5888 会追加 -1/-2，这类包同样有效。"""
        path = f"/sf/log/{TICKET}/{TICKET}-20261106155217-1.zip"

        archives = _parse_remote_archives(f"{path}\t100\t1793951537.0\n")

        self.assertEqual([item.path for item in archives], [path])

    def test_multiple_tickets_stay_independent(self) -> None:
        other = "Q2026110600999"
        output = (
            f"{CANON}\t100\t1793951537.0\n"
            f"{STAMPED}\t200\t1793951600.0\n"
            f"/sf/log/{other}/{other}.zip\t300\t1793951500.0\n"
        )

        archives = _parse_remote_archives(output)

        self.assertEqual([item.ticket_id for item in archives], [TICKET, other])
        self.assertEqual(archives[0].path, STAMPED)
        self.assertEqual(archives[1].path, f"/sf/log/{other}/{other}.zip")


class ResolveArchivePathTest(unittest.IsolatedAsyncioTestCase):
    """任务开始前解析该工单应拉取的远端路径。"""

    def setUp(self) -> None:
        self.cfg = SimpleNamespace(
            source="libo@192.168.0.2",
            source_port=22468,
            ssh_key="/tmp/id_ed25519",
            remote_archive=lambda tid: f"/sf/log/{tid}/{tid}.zip",
        )

    async def _resolve(self, stdout: bytes):
        seen: list[str] = []

        async def fake_ssh_run(cfg, command, message, timeout=20):
            seen.append(command)
            return stdout

        with patch("app.fetcher._ssh_run", fake_ssh_run):
            path = await resolve_archive_path(self.cfg, TICKET)
        return path, seen

    async def test_returns_stamped_newest_package(self) -> None:
        path, seen = await self._resolve(
            (
                f"{CANON}\t335882103\t1793936220.0\n"
                f"{STAMPED}\t571632020\t1793951537.0\n"
            ).encode()
        )

        self.assertEqual(path, STAMPED)
        # 枚举必须限制在单工单目录内，且不能递归
        self.assertIn(f"/sf/log/{TICKET}", seen[0])
        self.assertIn("-maxdepth 1", seen[0])

    async def test_returns_canonical_when_only_one_package(self) -> None:
        path, _ = await self._resolve(f"{CANON}\t1\t1793951537.0\n".encode())

        self.assertEqual(path, CANON)

    async def test_falls_back_to_canonical_when_directory_empty(self) -> None:
        """旧工单目录里可能只有非 zip 文件：回退规范名，让 rsync 报真实错误。"""
        path, _ = await self._resolve(b"")

        self.assertEqual(path, CANON)

    async def test_falls_back_to_canonical_when_ssh_fails(self) -> None:
        """枚举失败不能让任务直接失败：错误码只由随后的 rsync 决定。"""

        async def fake_ssh_run(cfg, command, message, timeout=20):
            raise TicketError(ErrorCode.SSH_FAILED, "无法枚举工单归档目录", "ssh exit=255")

        with patch("app.fetcher._ssh_run", fake_ssh_run):
            path = await resolve_archive_path(self.cfg, TICKET)

        self.assertEqual(path, CANON)


class RemoteSpecTest(unittest.TestCase):
    """rsync 远端描述的拼装：显式路径优先，本地落盘名不变。"""

    cfg = SimpleNamespace(source="libo@192.168.0.2")

    def test_uses_explicit_remote_path(self) -> None:
        self.assertEqual(
            _remote_spec(self.cfg, TICKET, STAMPED),
            f"libo@192.168.0.2:{STAMPED}",
        )

    def test_defaults_to_canonical_name(self) -> None:
        cfg = SimpleNamespace(
            source="libo@192.168.0.2",
            remote_archive=lambda tid: f"/sf/log/{tid}/{tid}.zip",
        )

        self.assertEqual(
            _remote_spec(cfg, TICKET, None),
            f"libo@192.168.0.2:{CANON}",
        )


class _FakeStream:
    """只实现 _pump 需要的一次性 read。"""

    def __init__(self, data: bytes = b"") -> None:
        self._data = data

    async def read(self, size: int) -> bytes:
        data, self._data = self._data, b""
        return data[:size]


class _FakeProcess:
    def __init__(self) -> None:
        self.stdout = _FakeStream()
        self.stderr = _FakeStream()
        self.returncode = 0

    async def wait(self) -> int:
        return 0


class FetchRemotePathTest(unittest.IsolatedAsyncioTestCase):
    """fetch 默认先解析最新包，但本地落盘名仍是规范名。"""

    def setUp(self) -> None:
        self.cfg = SimpleNamespace(
            source="libo@192.168.0.2",
            source_port=22468,
            ssh_key="/tmp/id_ed25519",
            max_archive_bytes=10**9,
            remote_archive=lambda tid: f"/sf/log/{tid}/{tid}.zip",
        )

    async def test_pulls_newest_package_but_writes_canonical_local_name(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            work_dir = Path(raw)
            launched: list[tuple] = []

            async def launcher(*args, **kwargs):
                launched.append(args)
                Path(args[-1]).write_bytes(b"x" * 16)  # 模拟 rsync 落盘
                return _FakeProcess()

            with (
                patch("app.fetcher.resolve_archive_path", AsyncMock(return_value=STAMPED)),
                patch("app.fetcher.asyncio.create_subprocess_exec", launcher),
            ):
                path = await fetch(TICKET, self.cfg, work_dir, "job-1")

            self.assertEqual(path, work_dir / f"{TICKET}.zip")
            self.assertIn(f"libo@192.168.0.2:{STAMPED}", launched[0])

    async def test_explicit_remote_path_skips_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            work_dir = Path(raw)
            resolver = AsyncMock(return_value=STAMPED)

            async def launcher(*args, **kwargs):
                Path(args[-1]).write_bytes(b"x" * 16)
                return _FakeProcess()

            with (
                patch("app.fetcher.resolve_archive_path", resolver),
                patch("app.fetcher.asyncio.create_subprocess_exec", launcher),
            ):
                await fetch(TICKET, self.cfg, work_dir, "job-2", remote_path=CANON)

            resolver.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
