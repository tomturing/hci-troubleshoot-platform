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


# ── 归档文件名契约（5888 只接受 <工单号>.zip）────────────────────────────────
#
# 回归护栏：曾用平台内部 task_id（UUID）当 filename，5888 返回 400
# Invalid filename，导致 P2 段③ 必然失败却只在"代传失败"层面暴露。
# 因此断言必须落在**服务层实际发出的请求体**上，而不是客户端的入参。


def _patch_http_transport(monkeypatch, handler):
    """把 relay 服务内部 new 出来的 httpx.AsyncClient 换成 MockTransport。"""

    real_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        return real_client(transport=httpx.MockTransport(handler), base_url="http://relay")

    monkeypatch.setattr(httpx, "AsyncClient", factory)


def _make_task(storage, case_id: str, payload: bytes = b"01234567"):
    task_id = uuid4()
    path = storage.object_path(case_id, str(task_id))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    task = SimpleNamespace(
        task_id=task_id,
        case_id=case_id,
        archive_path=f"{case_id}/{task_id}.zip",
        relay_upload_id=None,
        relay_chunk_size=None,
        relay_chunk_total=None,
        relay_chunk_index=None,
        relay_restarts=0,
    )
    return task, path


async def test_run_uses_ticket_number_as_archive_filename(storage, monkeypatch):
    case_id = "Q2026102700001"
    task, _ = _make_task(storage, case_id, b"01234567")
    captured = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/init_upload":
            captured["init_body"] = request.content
            return httpx.Response(200, json={"upload_id": "u-1", "chunk_size": CHUNK})
        if request.url.path == "/upload_chunk":
            captured["chunk_body"] = request.content
            return httpx.Response(200, json={"status": "complete"})
        if request.url.path.startswith("/upload_status/"):
            # 404 = 会话已被服务端删除 = 归档完成，_await_archive 立即返回
            return httpx.Response(404, json={"error": "Invalid or expired upload ID"})
        return httpx.Response(500)

    _patch_http_transport(monkeypatch, handler)
    service = LogCollectRelayService(
        make_factory(_FakeResult(scalar=task)), storage, SangforRelayClient("http://relay")
    )
    finished = {}
    service._finish = AsyncMock(
        side_effect=lambda task_id, status, error=None: finished.update(status=status, error=error)
    )

    await service.run(task.task_id)

    assert finished["status"] == "success"
    assert case_id.encode() + b".zip" in captured["init_body"]
    # 关键否定断言：平台内部 UUID 文件名绝不能再出现
    assert str(task.task_id).encode() not in captured["init_body"]
    assert case_id.encode() + b".zip" in captured["chunk_body"]


async def test_run_rejects_non_ticket_case_id_before_upload(storage, monkeypatch):
    task, _ = _make_task(storage, "Q1", b"01234567")
    called = []

    async def handler(request: httpx.Request) -> httpx.Response:
        called.append(request.url.path)
        return httpx.Response(200, json={"upload_id": "u-1", "chunk_size": CHUNK})

    _patch_http_transport(monkeypatch, handler)
    service = LogCollectRelayService(
        make_factory(_FakeResult(scalar=task)), storage, SangforRelayClient("http://relay")
    )
    service._finish = AsyncMock()

    with pytest.raises(SangforRelayError) as excinfo:
        await service.run(task.task_id)

    assert "工单号不合法" in str(excinfo.value)
    # 契约不符时不得先发一轮注定 400 的请求
    assert called == []
