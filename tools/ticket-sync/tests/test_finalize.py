"""收尾失败不得否定已发布的产物（2026-11-01）。

生产背景：`_run` 里 publish 之后还有三步收尾（判有无日志、写 meta、记账），
原本都放在同一个 try 里。任何一步抛异常都会落到兜底 except 把 job 标成 failed，
而产物其实已经完整落在 final_dir。

后果是双重误导：
- 自动巡检被 `final_dir.is_dir()` 挡住，不会重拉（歪打正着）；
- 手动 `POST /jobs` 只检查内存里的 RUNNING_PHASES，FAILED 允许重建 ——
  用户看到 failed 点一下重试，几十 GiB 的包就要再完整拉一遍。

约定：publish 返回之后，"这次同步成功了"就是既成事实，收尾失败只能追加告警。
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from app.main import app
from app.models import ExtractStats, Phase

TICKET_ID = "Q2026010100001"


async def _no_remote_entries(*_args, **_kwargs):
    """跳过下载前的归档内容探测：它只走 ssh，测试里用不上。"""
    return []


async def _fake_fetch(ticket_id, cfg, work_dir, job_id, on_progress=None):
    work_dir.mkdir(parents=True, exist_ok=True)
    archive = work_dir / f"{ticket_id}.zip"
    archive.write_bytes(b"zip")
    return archive


async def _fake_extract(
    ticket_id, archive, work_dir, cfg, job_id, on_progress=None, on_archive=None
):
    stats = ExtractStats()
    stats.depth = 1
    stats.files = 3
    stats.bytes = 123
    return stats


def _fake_publish(work_dir, final_dir, trash_dir):
    """模拟原子发布：产物落到 final_dir，work_dir 被移走。"""
    final_dir.mkdir(parents=True, exist_ok=True)
    (final_dir / "log_list.json").write_text("{}", encoding="utf-8")
    shutil.rmtree(work_dir, ignore_errors=True)
    return None


class FinalizeFailureTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.root = tempfile.mkdtemp(prefix="ticket-sync-finalize-")
        os.environ["TICKET_ROOT"] = self.root
        os.environ["TICKET_ARCHIVE_PASSWORD"] = "test-password"
        os.environ["TICKET_AUTO_SYNC_ENABLED"] = "false"
        # 手动驱动 lifespan：ASGITransport 不会替我们执行 startup/shutdown。
        self._lifespan = app.router.lifespan_context(app)
        await self._lifespan.__aenter__()
        self.registry = app.state.registry

    async def asyncTearDown(self) -> None:
        await self._lifespan.__aexit__(None, None, None)
        for key in ("TICKET_ROOT", "TICKET_ARCHIVE_PASSWORD", "TICKET_AUTO_SYNC_ENABLED"):
            os.environ.pop(key, None)
        shutil.rmtree(self.root, ignore_errors=True)

    async def _run_with(self, *extra):
        with ExitStack() as stack:
            stack.enter_context(patch("app.main.list_remote_entries", _no_remote_entries))
            stack.enter_context(patch("app.main.fetch", _fake_fetch))
            stack.enter_context(patch("app.main.extract", _fake_extract))
            stack.enter_context(patch("app.main.publish", _fake_publish))
            for patcher in extra:
                stack.enter_context(patcher)
            job = await self.registry.create(TICKET_ID)
            task = job.task
            assert task is not None
            await task
        return job

    async def test_meta_write_failure_keeps_job_ready(self) -> None:
        job = await self._run_with(
            patch("app.main.write_sync_meta", side_effect=OSError("No space left on device"))
        )

        self.assertEqual(job.phase, Phase.READY)
        self.assertIsNone(job.error_code)
        self.assertIn("元数据写入失败", job.message or "")
        # 产物必须还在：标 failed 的唯一后果就是诱导用户把那几十 GiB 重拉一遍
        self.assertTrue((Path(self.root) / TICKET_ID / "log_list.json").exists())

    async def test_successful_run_stays_quiet(self) -> None:
        """正常路径不能被这次改动污染成"永远带告警"。"""
        job = await self._run_with()

        self.assertEqual(job.phase, Phase.READY)
        self.assertFalse(job.message and "失败" in job.message)
        self.assertEqual(job.extracted_bytes, 123)
        self.assertEqual(job.file_count, 3)


if __name__ == "__main__":
    unittest.main()
