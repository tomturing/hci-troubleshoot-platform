"""5888 代传：协议客户端与状态抢占的行为测试。

主流程 `_run`（20 GiB 分片循环 + 归档轮询）依赖真实 5888 与 PostgreSQL，
由集成环境验证；这里锁定协议契约与最容易写错的边界。
"""

import asyncio
from datetime import UTC, datetime, timedelta
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
    def __init__(self, rowcount: int = 0, scalar=None, rows: list | None = None):
        self.rowcount = rowcount
        self._scalar = scalar
        self._rows = rows or []

    def scalar_one_or_none(self):
        return self._scalar

    def all(self):
        return self._rows


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
        relay_attempt=2,
        relay_last_error="5888 返回 503",
        relay_last_error_at=None,
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
    # 单片超时期间 chunk_index 不动，只有这两项能说明"进程还活着、上一轮为什么失败"。
    assert snapshot["relay_attempt"] == 2
    assert snapshot["relay_last_error"] == "5888 返回 503"


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
    service._finish = AsyncMock(
        side_effect=lambda task_id, status, error=None, error_code="LC-TRANSFER-003": finished.update(
            status=status, error=error, error_code=error_code
        )
    )

    with pytest.raises(SangforRelayError):
        await service.run(task.task_id)

    assert finished["status"] == "failed"
    assert "平台副本不存在" in finished["error"]
    # 错误码必须保留 SangforRelayError 自己的分类，不得被统一改写成 003
    assert finished["error_code"] == "LC-TRANSFER-001"


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


# ── 交付终态的持久化口径 ────────────────────────────────────────────────────


def _values_by_name(statement) -> dict:
    """把 update 语句的 values 摊成 {列名: 值}，避免依赖 Column 对象身份。"""
    # func.now() 这类服务端表达式没有 .value，原样返回即可（用例不断言它们）
    return {column.name: (getattr(bind, "value", bind)) for column, bind in statement._values.items()}


class _RecordingSession(_FakeSession):
    """记录 update 语句，用于断言落库字段（不连库）。"""

    def __init__(self, result: _FakeResult, statements: list):
        super().__init__(result)
        self._statements = statements

    async def execute(self, statement):
        self._statements.append(statement)
        return self.result


async def test_finish_marks_delivery_status(storage):
    """代传终态必须同时收敛 delivery_status。

    只写 relay_status 的话，delivery_status 会永久停在 begin_delivery 写的 queued，
    前端据此判断"是否已交付"永远拿不到 delivered——补交付入口会一直亮着，
    用户每次打开面板都被提示"上传到平台"，而包早就归档了。
    """
    statements: list = []
    service = LogCollectRelayService(
        lambda: _RecordingSession(_FakeResult(rowcount=1), statements),
        storage,
        SangforRelayClient("http://relay"),
    )

    await service._finish(uuid4(), "success")
    values = _values_by_name(statements[-1])
    assert values["delivery_status"] == "delivered"
    assert values["relay_status"] == "success"

    statements.clear()
    await service._finish(uuid4(), "failed", error="5888 无响应")
    values = _values_by_name(statements[-1])
    assert values["delivery_status"] == "failed"
    assert values["delivery_error"] == "5888 无响应"
    assert values["error_code"] == "LC-TRANSFER-003"


async def test_finish_preserves_original_error_code(storage):
    """失败终态必须保留异常自身的错误码。

    Q2026101500828 现场：relay 因"平台尚无制品"（LC-TRANSFER-001）失败，
    但 run() 把所有异常统一写成 LC-TRANSFER-003，前端只见"归档失败"，
    排障者无法区分"没制品"与"传输失败"——这正是死锁缺陷被掩盖 11 次的原因。
    """
    statements: list = []
    service = LogCollectRelayService(
        lambda: _RecordingSession(_FakeResult(rowcount=1), statements),
        storage,
        SangforRelayClient("http://relay"),
    )

    await service._finish(uuid4(), "failed", error="平台尚无制品，无法代传", error_code="LC-TRANSFER-001")
    values = _values_by_name(statements[-1])
    assert values["error_code"] == "LC-TRANSFER-001"
    assert values["error_message"] == "平台尚无制品，无法代传"
    assert values["delivery_status"] == "failed"


async def test_run_keeps_sangfor_relay_error_code(storage):
    """run() 捕获 SangforRelayError 时必须透传其 code，不做统一改写。"""
    statements: list = []
    service = LogCollectRelayService(
        lambda: _RecordingSession(_FakeResult(rowcount=1), statements),
        storage,
        SangforRelayClient("http://relay"),
    )

    async def boom(_task_id):
        raise SangforRelayError("平台尚无制品，无法代传", "LC-TRANSFER-001")

    service._run = boom  # type: ignore[method-assign]
    with pytest.raises(SangforRelayError):
        await service.run(uuid4())

    values = _values_by_name(statements[-1])
    assert values["relay_status"] == "failed"
    assert values["error_code"] == "LC-TRANSFER-001"


async def test_run_retries_transient_then_succeeds(storage):
    """瞬时错误（5888 临时不可达）自动重试，最终成功不应写 failed 终态。"""
    task, _ = _make_task(storage, "Q1", b"01234567")
    calls = {"n": 0}

    async def flaky_run(_tid):
        calls["n"] += 1
        if calls["n"] == 1:
            raise SangforRelayError("5888 无响应", "LC-TRANSFER-003")
        return None

    finished = {}
    service = LogCollectRelayService(
        make_factory(_FakeResult(scalar=task)),
        storage,
        SangforRelayClient("http://relay"),
        max_attempts=2,
        retry_base_delay=0.0,
    )
    service._run = flaky_run  # type: ignore[method-assign]
    service._finish = AsyncMock(
        side_effect=lambda task_id, status, error=None, error_code="LC-TRANSFER-003": finished.update(
            status=status, error=error, error_code=error_code
        )
    )
    await service.run(task.task_id)
    assert calls["n"] == 2
    assert finished == {}, "成功路径不应调用 _finish 写终态"


async def test_run_records_attempt_and_last_error_per_round(storage):
    """每轮开始落 attempt、失败时落 last_error：这是"卡住 vs 在传"的唯一区分依据。"""
    task, _ = _make_task(storage, "Q1", b"01234567")
    calls = {"n": 0}

    async def flaky_run(_tid):
        calls["n"] += 1
        if calls["n"] == 1:
            raise SangforRelayError("5888 超时", "LC-TRANSFER-003")

    saved: list[dict] = []

    async def fake_save(_task_id, **values):
        saved.append(values)

    service = LogCollectRelayService(
        make_factory(_FakeResult(scalar=task)),
        storage,
        SangforRelayClient("http://relay"),
        max_attempts=2,
        retry_base_delay=0.0,
    )
    service._run = flaky_run  # type: ignore[method-assign]
    service._save = fake_save  # type: ignore[method-assign]
    service._finish = AsyncMock()
    await service.run(task.task_id)

    assert {"attempt": 1} in saved
    assert {"attempt": 2} in saved, "第二轮也要落库，否则界面永远显示第 1 次"
    assert any("5888 超时" in str(v.get("last_error", "")) for v in saved), "失败原因必须落库"


async def test_run_transient_exhausted_marks_failed(storage):
    """瞬时错误重试耗尽后落 failed，且保留原始错误码。"""
    task, _ = _make_task(storage, "Q1", b"01234567")
    finished = {}
    service = LogCollectRelayService(
        make_factory(_FakeResult(scalar=task)),
        storage,
        SangforRelayClient("http://relay"),
        max_attempts=2,
        retry_base_delay=0.0,
    )
    service._run = AsyncMock(side_effect=SangforRelayError("5888 无响应", "LC-TRANSFER-003"))
    service._finish = AsyncMock(
        side_effect=lambda task_id, status, error=None, error_code="LC-TRANSFER-003": finished.update(
            status=status, error=error, error_code=error_code
        )
    )
    with pytest.raises(SangforRelayError):
        await service.run(task.task_id)
    assert finished["status"] == "failed"
    assert finished["error_code"] == "LC-TRANSFER-003"


async def test_run_permanent_error_not_retried(storage):
    """永久错误（无制品 LC-TRANSFER-001）立即落 failed，不进入重试。"""
    task, _ = _make_task(storage, "Q1", b"01234567")
    finished = {}
    service = LogCollectRelayService(
        make_factory(_FakeResult(scalar=task)),
        storage,
        SangforRelayClient("http://relay"),
        max_attempts=3,
        retry_base_delay=1.0,
    )
    service._run = AsyncMock(side_effect=SangforRelayError("平台尚无制品，无法代传", "LC-TRANSFER-001"))
    service._finish = AsyncMock(
        side_effect=lambda task_id, status, error=None, error_code="LC-TRANSFER-003": finished.update(
            status=status, error=error, error_code=error_code
        )
    )
    with pytest.raises(SangforRelayError):
        await service.run(task.task_id)
    assert finished["status"] == "failed"
    assert finished["error_code"] == "LC-TRANSFER-001"


# ── 出站前自证：平台副本必须与落库的制品一致 ────────────────────────────────


async def test_run_rejects_object_digest_mismatch_before_upload(storage, monkeypatch):
    """摘要不一致必须 fail-fast：5888 不回摘要，出站前是最后的拦截点。"""

    case_id = "Q2026102700002"
    task, _ = _make_task(storage, case_id, b"01234567")
    task.object_size = 8
    task.object_sha256 = "0" * 64  # 与真实内容不符
    called: list[str] = []

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

    assert excinfo.value.code == "LC-TRANSFER-002"
    assert "摘要与落库不一致" in str(excinfo.value)
    # 关键否定断言：不一致时一个字节都不该发给 5888
    assert called == []


async def test_run_rejects_object_size_mismatch_before_upload(storage, monkeypatch):
    case_id = "Q2026102700003"
    task, _ = _make_task(storage, case_id, b"01234567")
    task.object_size = 999  # 与磁盘实际大小不符
    called: list[str] = []

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

    assert excinfo.value.code == "LC-TRANSFER-002"
    assert "大小与落库不一致" in str(excinfo.value)
    assert called == []


# ── 租约：发布/重启后的可接管性 ────────────────────────────────────────────────
#
# 发布、OOM、SIGKILL 都会在 relay_status=running 时直接杀掉进程，没有任何人
# 会写终态。这些用例锁住三条不可回退的约束：
#   1. 租约过期的 running 必须能被普通 trigger 抢占（不依赖人工 force）；
#   2. 业务长阻塞期间必须持续续租，不能被误判为死进程；
#   3. 心跳/交还必须只作用于 running，不能复活终态或写坏 pending。


def _sql(statement) -> str:
    from sqlalchemy.dialects import postgresql

    return str(statement.compile(dialect=postgresql.dialect()))


def _set_clause(statement) -> str:
    """取 UPDATE 的 SET 段，用于断言「只改了什么」。"""

    sql = _sql(statement)
    return sql.split("SET", 1)[1].split("WHERE", 1)[0]


class _RecordingSession(_FakeSession):
    """记录执行过的语句：租约条件必须写进 SQL，而不是只被 Python 分支挡住。"""

    def __init__(self, result, sink: list):
        super().__init__(result)
        self._sink = sink

    async def execute(self, statement):
        self._sink.append(statement)
        return await super().execute(statement)


class _ScriptedSession(_FakeSession):
    """按调用顺序返回预设结果：select 给 rows，后续 update 给 rowcount。

    ``state`` 必须由调用方共享：服务每次 execute 都会新建 session，
    若脚本游标跟着 session 走，``trigger`` 会拿到 select 的结果而不是 update 的 rowcount。
    """

    def __init__(self, script: list, state: dict | None = None):
        self._script = script
        self._state = state if state is not None else {"index": 0}

    async def execute(self, statement):
        index = self._state["index"]
        result = self._script[min(index, len(self._script) - 1)]
        self._state["index"] = index + 1
        return result

    @property
    def _index(self) -> int:
        return self._state["index"]


def _lease_service(storage, result, sink: list, **kwargs):
    return LogCollectRelayService(
        lambda: _RecordingSession(result, sink), storage, SangforRelayClient("http://relay"), **kwargs
    )


async def test_trigger_claims_expired_running_lease(storage):
    sink: list = []
    service = _lease_service(storage, _FakeResult(rowcount=1), sink, stale_after=60)

    assert await service.trigger(uuid4()) is True

    sql = _sql(sink[0])
    # 非 force 也必须允许「running 但租约过期」被抢：这是发布后唯一的自动恢复入口
    assert "relay_updated_at <" in sql
    assert "relay_status =" in sql
    assert "relay_status IN" in sql


async def test_trigger_force_does_not_require_expired_lease(storage):
    sink: list = []
    service = _lease_service(storage, _FakeResult(rowcount=1), sink, stale_after=60)

    await service.trigger(uuid4(), force=True)

    sql = _sql(sink[0])
    # force 是「人已经判断它死了」，不该再被租约时间挡住
    assert "relay_updated_at <" not in sql
    assert "relay_status =" in sql


async def test_trigger_does_not_claim_fresh_lease(storage):
    """反向用例：两个实例同时跑同一任务会造成 20 GiB 双传。"""

    sink: list = []
    service = _lease_service(storage, _FakeResult(rowcount=0), sink, stale_after=60)

    assert await service.trigger(uuid4()) is False


def test_lease_expired_only_for_running(storage):
    service = LogCollectRelayService(
        make_factory(_FakeResult()), storage, SangforRelayClient("http://relay"), stale_after=60
    )
    now = datetime.now(UTC)

    assert service.lease_expired("running", None) is True
    assert service.lease_expired("running", now - timedelta(seconds=61)) is True
    assert service.lease_expired("running", now) is False
    # 终态与未开始永远不算过期，避免 reconcile 把已经交付成功的任务再拉起来
    assert service.lease_expired("success", now - timedelta(hours=5)) is False
    assert service.lease_expired("pending", None) is False


def test_lease_expired_accepts_naive_datetime(storage):
    """PG 驱动可能回 naive datetime：不能因此把租约判成永久有效。"""

    service = LogCollectRelayService(
        make_factory(_FakeResult()), storage, SangforRelayClient("http://relay"), stale_after=60
    )
    naive = (datetime.now(UTC) - timedelta(seconds=61)).replace(tzinfo=None)

    assert service.lease_expired("running", naive) is True


async def test_heartbeat_only_refreshes_timestamp(storage):
    sink: list = []
    service = _lease_service(storage, _FakeResult(rowcount=0), sink, stale_after=60)

    assert await service.heartbeat(uuid4()) is False

    set_clause = _set_clause(sink[0])
    assert "relay_updated_at" in set_clause
    # 关键否定断言：心跳写 relay_status 会把 pending/failed 复活成 running
    assert "relay_status" not in set_clause
    # 只给仍在 running 的任务续租
    assert "relay_status =" in _sql(sink[0]).split("WHERE", 1)[1]


async def test_heartbeat_loop_exits_when_lease_lost(storage):
    sink: list = []
    service = _lease_service(storage, _FakeResult(rowcount=0), sink, stale_after=1.0, heartbeat_interval=0.01)

    loop = asyncio.create_task(service._heartbeat_loop(uuid4()))
    await asyncio.sleep(0.05)

    # 状态已被终态/别的实例接管时必须停止续租，不能替别人把租约续下去
    assert loop.done() is True


async def test_heartbeat_loop_keeps_renewing_while_owned(storage):
    sink: list = []
    service = _lease_service(storage, _FakeResult(rowcount=1), sink, stale_after=1.0, heartbeat_interval=0.01)

    loop = asyncio.create_task(service._heartbeat_loop(uuid4()))
    await asyncio.sleep(0.05)
    still_running = not loop.done()
    loop.cancel()
    with pytest.raises(asyncio.CancelledError):
        await loop

    assert still_running is True
    assert len(sink) >= 2


async def test_claim_stale_selects_pending_and_expired_with_archive(storage):
    older, fresher = uuid4(), uuid4()
    script = [
        _FakeResult(rows=[(older,), (fresher,)]),
        _FakeResult(rowcount=1),
        _FakeResult(rowcount=0),
    ]
    state = {"index": 0}
    service = LogCollectRelayService(
        lambda: _ScriptedSession(script, state),
        storage,
        SangforRelayClient("http://relay"),
        stale_after=60,
    )

    claimed = await service.claim_stale()

    # 只有真正抢到的一条算本次执行；抢输的那条由对方负责
    assert claimed == [older]


async def test_claim_stale_skips_tasks_without_artifact(storage):
    session = _ScriptedSession([_FakeResult(rows=[])])
    service = LogCollectRelayService(
        lambda: session,
        storage,
        SangforRelayClient("http://relay"),
        stale_after=60,
    )

    assert await service.claim_stale() == []
    # 反向用例：没制品的 pending 是「还没合并完」的正常中间态，不该被 trigger
    assert session._index == 1


async def test_claim_stale_sql_requires_artifact_and_stale_lease(storage):
    statements: list = []

    from sqlalchemy.dialects import postgresql

    class _OneShot(_FakeSession):
        def __init__(self):
            self.calls = 0

        async def execute(self, statement):
            self.calls += 1
            statements.append(statement)
            if self.calls == 1:
                return _FakeResult(rows=[])
            return _FakeResult(rowcount=0)

    service = LogCollectRelayService(lambda: _OneShot(), storage, SangforRelayClient("http://relay"), stale_after=60)
    await service.claim_stale()

    sql = str(statements[0].compile(dialect=postgresql.dialect()))
    assert "archive_path IS NOT NULL" in sql
    assert "relay_updated_at <" in sql
    assert "relay_status =" in sql


async def test_release_hands_lease_back_as_pending(storage):
    sink: list = []
    ids = [uuid4(), uuid4()]
    service = _lease_service(storage, _FakeResult(rowcount=2), sink, stale_after=60)

    assert await service.release(ids) == 2

    from sqlalchemy.dialects import postgresql

    compiled = sink[0].compile(dialect=postgresql.dialect())
    assert compiled.params["relay_status"] == "pending"
    # 只交还仍在 running 的：已 success/failed 的不能被改回 pending
    assert "relay_status =" in _sql(sink[0]).split("WHERE", 1)[1]


async def test_release_without_targets_is_noop(storage):
    sink: list = []
    service = _lease_service(storage, _FakeResult(rowcount=0), sink, stale_after=60)

    assert await service.release([]) == 0
    assert sink == []


# ── 启动接管与退出交还：发布链路上的两个钩子 ──────────────────────────────


async def test_startup_reconcile_spawns_claimed_relays(monkeypatch):
    """启动时把上一个进程遗留的代传真的重新拉起，而不只是统计个数。"""

    from app.routes import log_collect as routes

    claimed = [uuid4(), uuid4()]
    monkeypatch.setattr(routes, "database_manager", SimpleNamespace(async_session_factory=lambda: None))
    monkeypatch.setattr(
        routes,
        "_build_relay_service",
        lambda: SimpleNamespace(claim_stale=AsyncMock(return_value=claimed)),
    )
    spawned: list = []
    monkeypatch.setattr(routes, "_spawn_relay", lambda _svc, task_id: spawned.append(task_id))

    await routes.reconcile_relays_on_startup()

    assert spawned == claimed


async def test_startup_reconcile_is_skipped_when_relay_disabled(monkeypatch):
    """反向用例：关闭代传功能时不能自作主张拉起任务。"""

    from app.routes import log_collect as routes

    monkeypatch.setattr(routes.settings, "LOG_COLLECT_RELAY_ENABLED", False)
    spawned: list = []
    monkeypatch.setattr(routes, "_spawn_relay", lambda _svc, task_id: spawned.append(task_id))

    await routes.reconcile_relays_on_startup()

    assert spawned == []


async def test_shutdown_hands_back_running_relays(monkeypatch):
    """退出时必须在关库前把租约交还，否则任务要等租约过期才能被接管。"""

    from app.routes import log_collect as routes

    task = asyncio.create_task(asyncio.sleep(3600))
    task_id = uuid4()
    released: list = []

    class _FakeRelay:
        async def release(self, ids):
            released.append(ids)
            return len(ids)

    monkeypatch.setattr(routes, "database_manager", SimpleNamespace(async_session_factory=lambda: None))
    monkeypatch.setattr(routes, "_build_relay_service", lambda: _FakeRelay())
    routes._running_relays[task] = task_id

    await routes.shutdown_relays()

    assert released == [[task_id]]
    assert task.cancelled() is True
    assert task_id not in routes._running_relays.values()
