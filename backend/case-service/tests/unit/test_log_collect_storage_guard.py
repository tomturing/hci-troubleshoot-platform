"""日志存储可写性与容量：必须报"具体问题"，不能退化成裸 500。

回归背景（2026-10-27 工单 Q2026102701294）：
case-service 以 uid=1000 运行，rootfs 的 /var/lib 不可写，P3 分片上传在
``LocalLogCollectStorage.__init__`` 里 mkdir 抛 PermissionError，最终以
``Request failed with status code 500`` 呈现给客户——既看不出是存储问题，
也不知道该改哪个目录。本文件锁定：

1. 目录创建失败 / 写探针失败 → ``LogCollectStorageUnavailableError``，文案含路径、uid、原因；
2. 剩余空间不足 → ``LogCollectStorageInsufficientError``，文案含所需与可用空间；
3. 上述异常经 service / route 后变成 LC-PLATFORM-007 / 008 + 503 + 可读文案。
"""

import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from app.routes import log_collect as route
from app.services.log_collect_storage import (
    LocalLogCollectStorage,
    LogCollectStorageInsufficientError,
    LogCollectStorageUnavailableError,
)
from app.services.log_collect_upload_service import LogCollectUploadError, LogCollectUploadService
from fastapi import HTTPException

CHUNK = 8


def _service(storage: LocalLogCollectStorage) -> LogCollectUploadService:
    repo = MagicMock()
    repo.flush = AsyncMock()
    repo.refresh = AsyncMock()
    return LogCollectUploadService(
        repo,
        storage,
        chunk_size=CHUNK,
        max_archive_bytes=1024,
        token_ttl_hours=1,
        secret=b"test-secret-32-bytes-padding-0000",
    )


# ── 存储层：不可写 ────────────────────────────────────────────────────────────


def test_mkdir_permission_denied_reports_path_uid_and_reason(tmp_path, monkeypatch):
    """目录创建失败：文案必须能直接指向"哪个目录、哪个用户、什么原因"。"""

    def _deny(self: Path, parents: bool = False, exist_ok: bool = False, mode: int = 0o777):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "mkdir", _deny)
    root = tmp_path / "log-collect"

    with pytest.raises(LogCollectStorageUnavailableError) as info:
        LocalLogCollectStorage(str(root))

    message = str(info.value)
    assert str(root) in message
    assert "权限" in message or "Permission denied" in message
    assert "uid=" in message
    assert "挂载" in message  # 处理建议必须在文案里，而不是让调用方自己猜


def test_write_probe_detects_readonly_directory(tmp_path, monkeypatch):
    """mkdir 成功但不可写（只读挂载 / ACL）：必须在构造期就暴露，而不是上传到一半。"""

    def _deny(self: Path, data: bytes):
        raise PermissionError(30, "Read-only file system")

    monkeypatch.setattr(Path, "write_bytes", _deny)
    with pytest.raises(LogCollectStorageUnavailableError) as info:
        LocalLogCollectStorage(str(tmp_path / "log-collect"))

    assert "Read-only file system" in str(info.value)


# ── 存储层 / 服务层：空间不足 ────────────────────────────────────────────────


def test_capacity_insufficient_carries_numbers(tmp_path, monkeypatch):
    storage = LocalLogCollectStorage(str(tmp_path / "log-collect"))
    monkeypatch.setattr(
        shutil, "disk_usage", lambda _path: SimpleNamespace(total=100, used=99, free=1)
    )

    with pytest.raises(LogCollectStorageInsufficientError) as info:
        storage.check_capacity(1024)

    message = str(info.value)
    assert "空间不足" in message
    assert "1.0 KB" in message  # 需要的量
    assert "1 B" in message  # 可用的量
    assert "清理" in message or "扩容" in message


async def test_init_maps_low_space_to_platform_code(tmp_path, monkeypatch):
    """service.init 必须把容量异常转成 LC-PLATFORM-008/503，而不是冒泡成 500。"""

    storage = LocalLogCollectStorage(str(tmp_path / "log-collect"))
    monkeypatch.setattr(
        shutil, "disk_usage", lambda _path: SimpleNamespace(total=100, used=99, free=1)
    )
    service = _service(storage)
    task = SimpleNamespace(task_id=uuid4(), case_id="Q2026102701294")

    with pytest.raises(LogCollectUploadError) as info:
        await service.init(task, total_size=64, expected_sha256=None)

    assert info.value.code == "LC-PLATFORM-008"
    assert info.value.status == 503
    assert "空间不足" in str(info.value)


# ── 路由层：存储不可用 → LC-PLATFORM-007 ────────────────────────────────────


def test_get_upload_storage_maps_unavailable_to_problem(monkeypatch):
    """依赖注入阶段就必须产出 RFC 7807 problem（现场就是在这里变成裸 500 的）。"""

    def _boom(_root: str):
        raise LogCollectStorageUnavailableError(Path("/var/lib/log-collect"), PermissionError(13, "Permission denied"))

    monkeypatch.setattr(route, "upload_storage", None)
    monkeypatch.setattr(route, "LocalLogCollectStorage", _boom)

    with pytest.raises(HTTPException) as info:
        route.get_upload_storage()

    assert info.value.status_code == 503
    problem = info.value.detail
    assert problem["code"] == "LC-PLATFORM-007"
    assert problem["title"] == "日志存储目录不可写"
    assert "/var/lib/log-collect" in problem["detail"]
    assert problem["user_hint"]  # 前端可直接展示"怎么办"
    # 失败后不得把半成品实例缓存下来，下一次请求要能重新尝试（修好卷即自愈）
    assert route.upload_storage is None
