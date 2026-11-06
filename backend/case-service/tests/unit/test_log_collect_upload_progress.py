"""分片上传进度回写平台的边界测试。

为什么单独测这块：交付卡在弱网上时，平台侧 ``delivery_status`` 一直停在 queued、
``object_size`` 恒空，如果连"已收到第几片"都没有，界面就完全无法区分
"再等等"和"这条链已经废了"。2026-11-06 Q2026110600112 卡在 19/233 十小时，
现场只能看到「交付已登记但未完成」，就是这么来的。

同时这里钉住一条硬约束：进度回写失败**绝不能**让分片上传失败——否则一个
UPDATE 抖动就会让客户端重传整片，在弱网上白烧一次宝贵的上行带宽。
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from app.services import log_collect_upload_service as upload_module
from app.services.log_collect_storage import LocalLogCollectStorage
from app.services.log_collect_upload_service import (
    LogCollectUploadService,
    should_report_upload_progress,
)

CHUNK = 8
PART_COUNT = 5


@pytest.fixture(autouse=True)
def _reset_throttle_state():
    """节流状态是模块级全局，必须逐用例清空，否则用例之间会互相污染。"""
    upload_module._upload_progress_seen.clear()
    yield
    upload_module._upload_progress_seen.clear()


@pytest.fixture
def storage(tmp_path):
    return LocalLogCollectStorage(str(tmp_path / "log-collect"))


@pytest.fixture
def repo():
    repo = MagicMock()
    repo.flush = AsyncMock()
    repo.refresh = AsyncMock()
    repo.commit = AsyncMock()
    # 进度回写是新增的异步方法，给出显式 AsyncMock，
    # 否则 MagicMock 的隐式异步协议会让调用静默失败、断言落空。
    repo.update_upload_progress = AsyncMock()
    return repo


@pytest.fixture
def service(storage, repo):
    return LogCollectUploadService(
        repo,
        storage,
        chunk_size=CHUNK,
        max_archive_bytes=1024,
        token_ttl_hours=1,
        secret=b"test-secret-32-bytes-padding-0000",
    )


def make_task(total_size: int = CHUNK * PART_COUNT):
    return SimpleNamespace(
        task_id=uuid4(),
        case_id="Q2026110600112",
        archive_path=None,
        archive_size=None,
        archive_sha256=None,
    )


async def _chunks(payload: bytes, block: int = 3):
    for start in range(0, len(payload), block):
        yield payload[start : start + block]


async def test_first_part_reports_progress(service, repo):
    task = make_task()
    await service.init(task, total_size=CHUNK * PART_COUNT, expected_sha256=None)

    await service.record_part(task.task_id, part_number=1, chunks=_chunks(b"a" * CHUNK), claimed_sha256=None)

    repo.update_upload_progress.assert_awaited_once()
    task_id_arg, progress = repo.update_upload_progress.await_args.args
    assert task_id_arg == task.task_id
    assert progress["uploaded"] == 1
    assert progress["total"] == PART_COUNT
    assert progress["chunk_size"] == CHUNK
    assert progress["last_part"] == 1
    assert "updated_at" in progress


async def test_progress_is_throttled_until_enough_parts(service, repo):
    task = make_task()
    await service.init(task, total_size=CHUNK * PART_COUNT, expected_sha256=None)

    # 第 1 片必报（首次），随后 2/3/4 片在 2 秒内不重复写库。
    for number in range(1, PART_COUNT):
        await service.record_part(
            task.task_id, part_number=number, chunks=_chunks(bytes([64 + number]) * CHUNK), claimed_sha256=None
        )
    assert repo.update_upload_progress.await_count == 1

    # 末片是状态跃迁点，必须再报一次：界面不能停在 1/5 让人以为还要等 4 片。
    await service.record_part(
        task.task_id, part_number=PART_COUNT, chunks=_chunks(b"z" * CHUNK), claimed_sha256=None
    )
    assert repo.update_upload_progress.await_count == 2
    assert repo.update_upload_progress.await_args.args[1]["uploaded"] == PART_COUNT


async def test_progress_failure_does_not_break_part_upload(service, repo):
    repo.update_upload_progress = AsyncMock(side_effect=RuntimeError("数据库抖了一下"))
    task = make_task()
    await service.init(task, total_size=CHUNK * PART_COUNT, expected_sha256=None)

    result = await service.record_part(
        task.task_id, part_number=1, chunks=_chunks(b"a" * CHUNK), claimed_sha256=None
    )

    # 字节已经落盘，返回值必须正常；只有进度缺失。
    assert result["uploaded_parts"] == 1
    assert result["part_number"] == 1


def test_should_report_upload_progress_boundaries():
    # 首次必须上报。
    assert should_report_upload_progress(None, uploaded=1, now=100.0) is True
    # 未攒够 5 片且不到 2 秒：跳过。
    assert should_report_upload_progress((10, 100.0), uploaded=12, now=101.0) is False
    # 攒够 5 片：立即上报，不必等时间。
    assert should_report_upload_progress((10, 100.0), uploaded=15, now=100.1) is True
    # 时间到了：即使没攒够也上报，保证界面有活着的证据。
    assert should_report_upload_progress((10, 100.0), uploaded=11, now=102.5) is True
    # 刚好卡在 2 秒边界上应上报（避免浮点比较导致迟一秒）。
    assert should_report_upload_progress((10, 100.0), uploaded=11, now=102.0) is True
    # 末片必报：5 片的包第 5 片不能因为"没攒够 5 片"而被吞掉。
    assert should_report_upload_progress((1, 100.0), uploaded=5, now=100.1, total=5) is True