"""交付一致性回归护栏：会话代际 fencing 与单一权威终态。

背景（2026-11-06 Q2026110601054）：
- 重新采集覆盖同名包后，旧会话仍按提交时冻结的 total_size 校验；
- 被顶替的旧作业复活并把 fatal 写回平台任务；
- relay 已 delivered、包已落 5888，前端却因 WS 断连写了 status=failed。

本文件锁定三条底层约束：内容身份分叉必须重建会话、旧代令牌必须失效、
客户端不得写交付终态。
"""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from app.models.log_collect import LogCollectTask
from app.repositories.log_collect_repo import LogCollectTaskRepository
from app.schemas.log_collect import LogCollectTaskPatch
from app.services.log_collect_service import LogCollectService
from app.services.log_collect_storage import LocalLogCollectStorage
from app.services.log_collect_upload_service import (
    LogCollectUploadService,
    PartValidationError,
)

SECRET = b"test-secret-32-bytes-padding-0000"


@pytest.fixture
def storage(tmp_path):
    return LocalLogCollectStorage(str(tmp_path / "log-collect"))


@pytest.fixture
def upload_service(storage):
    repo = MagicMock()
    repo.flush = AsyncMock()
    repo.refresh = AsyncMock()
    repo.commit = AsyncMock()
    return LogCollectUploadService(
        repo,
        storage,
        chunk_size=8,
        max_archive_bytes=1024,
        token_ttl_hours=1,
        secret=SECRET,
    )


@pytest.fixture
def upload_task():
    return SimpleNamespace(
        task_id=uuid4(),
        case_id="Q2026110601054",
        trace_id="tr-delivery-consistency",
    )


@pytest.mark.asyncio
async def test_legacy_session_keeps_old_token_format(upload_service, upload_task):
    """老前端不传代际时，令牌格式必须完全不变，否则在途上传会被 401 打断。"""

    session = await upload_service.init(upload_task, total_size=16, expected_sha256=None)
    assert session["delivery_epoch"] == 0
    assert upload_service.verify_token(str(upload_task.task_id), session["upload_token"])


@pytest.mark.asyncio
async def test_old_generation_token_is_fenced(upload_service, upload_task, storage):
    """会话重建后，旧代令牌必须失效，防止旧作业分片混进新成品。"""

    session = await upload_service.init(
        upload_task, total_size=16, expected_sha256=None, delivery_epoch=1
    )
    assert upload_service.verify_token(str(upload_task.task_id), session["upload_token"])

    meta = storage.read_meta(str(upload_task.task_id))
    meta["delivery_epoch"] = 2
    storage.write_meta(str(upload_task.task_id), meta)

    assert not upload_service.verify_token(str(upload_task.task_id), session["upload_token"])
    fresh_token = upload_service.issue_token(str(upload_task.task_id), 2)
    assert upload_service.verify_token(str(upload_task.task_id), fresh_token)


@pytest.mark.asyncio
async def test_same_size_but_changed_fingerprint_requires_rebuild(upload_service, upload_task):
    """同名换包但字节数不变时，也必须识别为内容分叉（不能只比 total_size）。"""

    await upload_service.init(
        upload_task, total_size=16, expected_sha256=None, archive_fingerprint="16:/a.zip:gen1"
    )
    with pytest.raises(PartValidationError) as exc:
        await upload_service.init(
            upload_task,
            total_size=16,
            expected_sha256=None,
            archive_fingerprint="16:/a.zip:gen2",
            delivery_epoch=1,
        )
    assert getattr(exc.value, "status", None) == 409


@pytest.fixture
def task_service():
    repo = MagicMock(spec=LogCollectTaskRepository)
    repo.get_by_id = AsyncMock()
    repo.flush = AsyncMock()
    repo.refresh = AsyncMock()
    repo.commit = AsyncMock()
    return LogCollectService(repo), repo


def _task(**overrides):
    task = MagicMock(spec=LogCollectTask)
    values = dict(
        status="uploading",
        delivery_status="none",
        delivery_error=None,
        relay_status="none",
        progress=50,
        detail={},
        trace_id="tr-delivery-consistency",
        upload_mode="bridge_relay",
    )
    values.update(overrides)
    for key, value in values.items():
        setattr(task, key, value)
    return task


@pytest.mark.asyncio
async def test_client_cannot_write_delivery_terminal_fields(task_service):
    """公共 PATCH 静默忽略客户端写的交付终态，且不影响其它字段。"""

    service, repo = task_service
    task = _task()
    repo.get_by_id.return_value = task

    await service.update_task(
        uuid4(),
        LogCollectTaskPatch(
            delivery_status="delivered",
            delivery_error="客户端猜测",
            status="uploading",
        ),
    )
    assert task.delivery_status == "none"
    assert task.delivery_error is None


@pytest.mark.asyncio
async def test_delivered_fact_cannot_be_reverted_to_failed(task_service):
    """已交付是持久化事实，WS 断连等客户端误判不得把它回退成 failed。"""

    service, repo = task_service
    task = _task(delivery_status="delivered")
    repo.get_by_id.return_value = task

    await service.update_task(
        uuid4(),
        LogCollectTaskPatch(status="failed", error_message="与 terminal_bridge 的连接已断开"),
    )
    assert task.status == "uploading"


@pytest.mark.asyncio
async def test_server_side_archive_verification_writes_terminal(task_service):
    """服务端核验归档后仍必须能写终态（single-writer 的权威入口）。"""

    service, repo = task_service
    task = _task()
    repo.get_by_id.return_value = task

    await service.update_task(
        uuid4(),
        LogCollectTaskPatch(delivery_status="delivered"),
        server_authoritative=True,
    )
    assert task.delivery_status == "delivered"


@pytest.mark.asyncio
async def test_p1_delivered_clears_stale_attest_error_code(task_service):
    """P1 直传补登 delivered 时，必须清掉之前残留的核验否定失败码。

    现场 Q2026110666667 / b7fa86c5：归档核验曾判 stale_archive 写下
    LC-TRANSFER-007，后续节点真实直传成功补登 delivered，error_code 却
    留着未清，造成 delivered 与失败码并存的矛盾记录。
    """

    service, repo = task_service
    task = _task(
        upload_mode="acli_direct",
        status="collecting",
        delivery_status="failed",
        error_code="LC-TRANSFER-007",
        error_message="P1 归档核验未通过：stale_archive",
    )
    repo.get_by_id.return_value = task

    await service.update_task(
        uuid4(),
        LogCollectTaskPatch(
            delivery_status="delivered",
            delivery_finished_at=datetime.now(UTC),
        ),
        server_authoritative=True,
    )
    assert task.delivery_status == "delivered"
    assert task.error_code is None
    assert task.error_message is None
