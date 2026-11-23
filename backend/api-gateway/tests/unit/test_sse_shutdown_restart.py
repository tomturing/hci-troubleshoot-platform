"""网关退出时在途 SSE 的收盘行为单测。

网关只是透传字节，所以上游的 server_restart 本来就能穿过；真正缺的是**网关自己**
被替换时的收盘能力：没有它，grace(60s) 到期后 TCP 被直接切断，前端拿不到任何事件、
也无法重连。这里锁定两点：

1. 正常流必须逐字节原样透传（不能因为引入了 pull 循环就改写内容）。
2. 退出标志置位后，必须补一条 server_restart 并结束，且不能卡在"上游无数据"的等待上。
"""

import asyncio

import pytest
from app.routes import conversations as conv_routes
from app.services.shutdown_signal import SERVER_RESTART_FRAME, shutdown_signal


class _FakeResponse:
    """只实现 _iter_upstream_sse 用到的 aiter_bytes。"""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk


class _IdleResponse:
    """永远没有新字节——模拟 ops-agent 正在思考、上游长时间静默。"""

    async def aiter_bytes(self):
        await asyncio.Event().wait()
        yield b""  # pragma: no cover - 永远不会到这里


async def _collect(gen) -> list[bytes]:
    return [chunk async for chunk in gen]


@pytest.fixture(autouse=True)
def _reset_signal(monkeypatch):
    shutdown_signal._stopping = False
    shutdown_signal._closed_streams = 0
    # 轮询间隔压到 10ms，避免单测真的等 1 秒。
    monkeypatch.setattr(conv_routes, "STOP_POLL_INTERVAL_SECONDS", 0.01)
    yield
    shutdown_signal._stopping = False


@pytest.mark.asyncio
async def test_stream_passes_through_bytes_when_not_stopping():
    response = _FakeResponse([b"data: a\n\n", b"data: b\n\n"])

    chunks = await _collect(conv_routes._iter_upstream_sse(response, url="http://up/sse"))

    assert chunks == [b"data: a\n\n", b"data: b\n\n"]


@pytest.mark.asyncio
async def test_idle_upstream_gets_server_restart_on_shutdown():
    response = _IdleResponse()
    gen = conv_routes._iter_upstream_sse(response, url="http://up/sse")

    async def _stop_soon():
        await asyncio.sleep(0.05)
        shutdown_signal.mark_stopping()

    stopper = asyncio.create_task(_stop_soon())
    try:
        chunks = await asyncio.wait_for(_collect(gen), timeout=2.0)
    finally:
        await stopper

    assert chunks == [SERVER_RESTART_FRAME]
    # 收盘时记过一次，这里再记一次即 2，证明 note_stream_closed 每次都递增。
    assert shutdown_signal.note_stream_closed() == 2


@pytest.mark.asyncio
async def test_shutdown_mid_stream_keeps_already_sent_bytes_then_restarts():
    """已经吐给客户端的字节不能丢——只有它们之后才追加收盘帧。"""
    response = _FakeResponse([b"data: a\n\n", b"data: b\n\n"])
    shutdown_signal.mark_stopping()

    chunks = await _collect(conv_routes._iter_upstream_sse(response, url="http://up/sse"))

    # 第一帧已经拿到但立刻发现退出：保留它再补收盘帧，避免前端丢内容。
    assert chunks == [b"data: a\n\n", SERVER_RESTART_FRAME]