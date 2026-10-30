"""回归测试：阈值定标与工具链加固（2026-10-30）。

对应三个生产遗留：
1. 嵌套层数上限从「整单失败」改为「停止下钻」——主体日志不能因为几个深层附件全丢；
2. RAR5 优先用 rarlab 的 unrar（p7zip 23.01 对 v50 -m3 报 Unsupported Method）；
3. ssh 连接层失败（exit=255）重试一次，且失败信息必须可归因（旧实现 stderr 为空）。
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app import extractor, fetcher
from app.config import Config
from app.models import ErrorCode, TicketError

TICKET_ID = "Q2026010100001"


def _make_config(root: Path, max_depth: int = 12) -> Config:
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
        min_free_bytes=0,
        max_files=500_000,
        max_depth=max_depth,
        concurrency=1,
        auto_sync_enabled=False,
        auto_sync_interval_seconds=30,
        retention_days=7,
    )


class DepthTruncationTest(unittest.IsolatedAsyncioTestCase):
    """层数上限只停止下钻，不判整单失败。"""

    async def asyncSetUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="ticket-sync-depth-"))
        self.work_dir = self.root / ".tmp" / "work"
        self.work_dir.mkdir(parents=True)

    async def asyncTearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    async def test_depth_limit_keeps_extracted_output(self) -> None:
        """达到层数上限时必须保留已解产物，并统计被截断的归档数。

        旧实现直接 raise DEPTH_EXCEEDED，调用方回滚整个工作目录 → 用户拿不到
        任何日志。实测最深 7 层、历史阈值 4 层，这种失败在生产上等于整单不可用。
        """
        archive = self.work_dir / f"{TICKET_ID}.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("log_list.json", "{}")
            # 必须大于占位包阈值（64 字节），否则会被当作采集端坏包剔除。
            zf.writestr("deep.zip", b"PK\x03\x04" + b"\x00" * 200)
        cfg = _make_config(self.root, max_depth=1)

        stats = await extractor.extract(TICKET_ID, archive, self.work_dir, cfg, "job-depth")

        self.assertEqual(stats.depth_truncated, 1)
        self.assertTrue((self.work_dir / "log_list.json").is_file())
        # 深层归档既不解压也不删除：留原件供人工处理
        self.assertTrue((self.work_dir / "deep.zip").is_file())


class UnrarPreferenceTest(unittest.IsolatedAsyncioTestCase):
    """RAR 解压的双通道顺序与回退条件。"""

    async def test_prefers_unrar_binary_when_available(self) -> None:
        with (
            patch.object(extractor.shutil, "which", return_value="/usr/bin/unrar"),
            patch.object(extractor, "_unrar_binary", new=AsyncMock()) as binary,
            patch.object(extractor, "_unrar_7z", new=AsyncMock()) as fallback,
        ):
            await extractor._unrar(Path("/tmp/x.rar"), Path("/tmp/out"), "")
        binary.assert_awaited_once()
        fallback.assert_not_awaited()

    async def test_falls_back_to_7z_when_unrar_fails(self) -> None:
        """两者覆盖的方法集合不同：unrar 失败不代表 7z 也失败。"""
        with (
            patch.object(extractor.shutil, "which", return_value="/usr/bin/unrar"),
            patch.object(
                extractor,
                "_unrar_binary",
                new=AsyncMock(
                    side_effect=TicketError(ErrorCode.EXTRACT_FAILED, "解压失败", "unsupported")
                ),
            ),
            patch.object(extractor, "_unrar_7z", new=AsyncMock()) as fallback,
        ):
            await extractor._unrar(Path("/tmp/x.rar"), Path("/tmp/out"), "")
        fallback.assert_awaited_once()

    async def test_bad_password_does_not_waste_a_second_tool(self) -> None:
        """密码错是归档本身的问题，换工具没有意义，应立即上抛。"""
        with (
            patch.object(extractor.shutil, "which", return_value="/usr/bin/unrar"),
            patch.object(
                extractor,
                "_unrar_binary",
                new=AsyncMock(
                    side_effect=TicketError(ErrorCode.BAD_PASSWORD, "归档密码错误", "unrar exit=10")
                ),
            ),
            patch.object(extractor, "_unrar_7z", new=AsyncMock()) as fallback,
        ):
            with self.assertRaises(TicketError):
                await extractor._unrar(Path("/tmp/x.rar"), Path("/tmp/out"), "pw")
        fallback.assert_not_awaited()

    async def test_no_unrar_falls_back_to_7z(self) -> None:
        with (
            patch.object(extractor.shutil, "which", return_value=None),
            patch.object(extractor, "_unrar_7z", new=AsyncMock()) as fallback,
        ):
            await extractor._unrar(Path("/tmp/x.rar"), Path("/tmp/out"), "")
        fallback.assert_awaited_once()


class _FakeProc:
    """最小的子进程替身：只提供 _ssh_run 用到的三个成员。"""

    def __init__(self, code: int, stdout: bytes = b"", stderr: bytes = b"") -> None:
        self.returncode = code
        self._stdout = stdout
        self._stderr = stderr

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr

    def kill(self) -> None:
        return None

    async def wait(self) -> None:
        return None


class SshRetryTest(unittest.IsolatedAsyncioTestCase):
    """连接层失败重试一次，且 detail 必须能定位到「哪一步、第几次、多久」。"""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="ticket-sync-ssh-"))
        self.cfg = _make_config(self.root)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    async def test_connect_failure_retries_once_then_succeeds(self) -> None:
        procs = [_FakeProc(255), _FakeProc(0, stdout=b"a.zip\t1\t2\n")]
        with (
            patch.object(fetcher.asyncio, "create_subprocess_exec", new=AsyncMock(side_effect=procs)),
            patch.object(fetcher.asyncio, "sleep", new=AsyncMock()) as sleep,
        ):
            stdout = await fetcher._ssh_run(self.cfg, "true", "测试命令")
        self.assertEqual(stdout, b"a.zip\t1\t2\n")
        sleep.assert_awaited_once()

    async def test_double_failure_reports_attempt_and_empty_stderr(self) -> None:
        """生产事故 detail 是 `ssh exit=255 stderr=`——stderr 为空时必须显式标注。"""
        procs = [_FakeProc(255), _FakeProc(255)]
        with (
            patch.object(fetcher.asyncio, "create_subprocess_exec", new=AsyncMock(side_effect=procs)),
            patch.object(fetcher.asyncio, "sleep", new=AsyncMock()),
        ):
            with self.assertRaises(TicketError) as ctx:
                await fetcher._ssh_run(self.cfg, "true", "无法扫描日志源服务器")

        self.assertEqual(ctx.exception.code, ErrorCode.SSH_FAILED)
        self.assertIn("attempt=2", ctx.exception.detail)
        self.assertIn("acli@192.168.0.2", ctx.exception.detail)
        self.assertIn("(空)", ctx.exception.detail)

    async def test_non_connect_failure_is_not_retried(self) -> None:
        """远端命令自身报错（如 unzip 缺失）重试无意义，只会拖长故障时间。"""
        procs = [_FakeProc(1, stderr=b"unzip: not found")]
        with (
            patch.object(fetcher.asyncio, "create_subprocess_exec", new=AsyncMock(side_effect=procs)),
            patch.object(fetcher.asyncio, "sleep", new=AsyncMock()) as sleep,
        ):
            with self.assertRaises(TicketError):
                await fetcher._ssh_run(self.cfg, "true", "测试命令")
        sleep.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
