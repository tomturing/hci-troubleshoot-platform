"""5888 代传：协议客户端与状态抢占的行为测试。

主流程 `_run`（20 GiB 分片循环 + 归档轮询）依赖真实 5888 与 PostgreSQL，
由集成环境验证；这里锁定协议契约与最容易写错的边界。
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from app.services.log_collect_relay_service import (
    LogCollectRelayService,
    SangforRelayClient,
    SangforRelayError,
)

CHUNK = 8


def make_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://relay")


class _FakeResult:
    def __init__(self, rowcount: int = 0, scalar=None):
        self.rowcount = rowcount
        self._scalar = scalar

    def scalar_one_or_none(self):
        return self._scalar


class _FakeSession:
    def __init__(self, result: _FakeResult):
        self.result = result

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, statement):
        return self.result

    async def commit(self):
        return None


def make_factory(result: _FakeResult):
    return lambda: _FakeSession(result)


@pytest.fixture
def storage(tmp_path):
    from app.services.log_collect_storage import LocalLogCollectStorage

    return LocalLogCollectStorage(str(tmp_path / "lc"))


# ── 协议客户端 ──────────────────────────────────────────────────────────────


async def test_init_upload_returns_id_and_chunk_size():
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/init_upload"
        return httpx.Response(200, json={"upload_id": "u-1", "chunk_size": CHUNK})

    client = SangforRelayClient("http://relay/")
    async with make_client(handler) as http:
        upload_id, chunk_size = await client.init_upload(http, "Q1.zip", 20)

    assert upload_id == "u-1"
    assert chunk_size == CHUNK


async def test_init_upload_rejects_invalid_payload():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"upload_id": "", "chunk_size": 0})

    client = SangforRelayClient("http://relay")
    async with make_client(handler) as http:
        with pytest.raises(SangforRelayError):
            await client.init_upload(http, "Q1.zip", 20)


async def test_upload_chunk_returns_status():
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["ctype"] = request.headers.get("content-type", "")
        return httpx.Response(200, json={"status": "complete"})

    client = SangforRelayClient("http://relay")
    async with make_client(handler) as http:
        status = await client.upload_chunk(
            http,
            upload_id="u-1",
            chunk_index=0,
            chunk_total=1,
            filename="Q1.zip",
            payload=b"abcdefgh",
        )

    assert status == "complete"
    assert captured["path"] == "/upload_chunk"
    assert captured["ctype"].startswith("multipart/form-data")


async def test_upload_status_returns_none_on_404():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "Invalid or expired upload ID"})

    client = SangforRelayClient("http://relay")
    async with make_client(handler) as http:
        assert await client.upload_status(http, "u-1") is None


async def test_upload_status_returns_progress():
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/upload_status/u-1"
        return httpx.Response(200, json={"uploaded_size": 8, "file_size": 20, "progress": 40.0})

    client = SangforRelayClient("http://relay")
    async with make_client(handler) as http:
        payload = await client.upload_status(http, "u-1")

    assert payload is not None
    assert payload["progress"] == 40.0


# ── 抢占与快照 ──────────────────────────────────────────────────────────────


async def test_trigger_claims_when_not_running(storage):
    service = LogCollectRelayService(make_factory(_FakeResult(rowcount=1)), storage, SangforRelayClient("http://relay"))
    assert await service.trigger(uuid4()) is True


async def test_trigger_returns_false_when_already_running(storage):
    service = LogCollectRelayService(make_factory(_FakeResult(rowcount=0)), storage, SangforRelayClient("http://relay"))
    assert await service.trigger(uuid4()) is False


async def test_snapshot_maps_relay_fields(storage):
    task = SimpleNamespace(
        task_id=uuid4(),
        relay_status="running",
        relay_upload_id="u-1",
        relay_chunk_size=CHUNK,
        relay_chunk_index=3,
        relay_chunk_total=10,
        relay_restarts=1,
        relay_updated_at=None,
        archive_path="Q1/t.zip",
    )
    service = LogCollectRelayService(
        make_factory(_FakeResult(scalar=task)), storage, SangforRelayClient("http://relay")
    )
    snapshot = await service.snapshot(task.task_id)

    assert snapshot is not None
    assert snapshot["relay_status"] == "running"
    assert snapshot["relay_chunk_index"] == 3
    assert snapshot["relay_restarts"] == 1


# ── 分片读取边界 ────────────────────────────────────────────────────────────


def test_read_chunk_handles_last_partial_chunk(tmp_path):
    path = tmp_path / "a.zip"
    path.write_bytes(b"0123456789abcdefghij")  # 20 字节

    assert LogCollectRelayService._read_chunk(path, 0, CHUNK, 20) == b"01234567"
    assert LogCollectRelayService._read_chunk(path, 1, CHUNK, 20) == b"89abcdef"
    assert LogCollectRelayService._read_chunk(path, 2, CHUNK, 20) == b"ghij"
    # 越界返回空，调用方据此报错而不是发送空分片
    assert LogCollectRelayService._read_chunk(path, 3, CHUNK, 20) == b""


# ── 失败收尾 ────────────────────────────────────────────────────────────────


async def test_run_marks_failed_when_archive_missing(storage):
    task = SimpleNamespace(
        task_id=uuid4(),
        case_id="Q1",
        archive_path="Q1/t.zip",
        relay_upload_id=None,
        relay_chunk_size=None,
        relay_chunk_total=None,
        relay_chunk_index=None,
        relay_restarts=0,
    )
    finished = {}

    service = LogCollectRelayService(
        make_factory(_FakeResult(scalar=task)), storage, SangforRelayClient("http://relay")
    )
    service._finish = AsyncMock(side_effect=lambda task_id, status, error=None: finished.update(status=status, error=error))

    with pytest.raises(SangforRelayError):
        await service.run(task.task_id)

    assert finished["status"] == "failed"
    assert "平台副本不存在" in finished["error"]
