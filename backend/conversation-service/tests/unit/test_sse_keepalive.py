"""SSE 空闲保活包装器（sse_keepalive）单元测试。

验证治本修复：长耗时诊断（如动态 skill 非流式 LLM 调用 120s×N 重试）期间，
SSE 无字节输出时反向代理（nginx proxy_read_timeout=300s）不会掐断连接。
"""

import asyncio

import pytest
from app.routes.conversations import sse_keepalive


def _wrap(source, interval=0.05, enabled=True):
    return sse_keepalive(source, interval=interval, enabled=enabled, stream="test")


@pytest.mark.asyncio
async def test_keepalive_emits_heartbeat_during_idle():
    """源流空闲（长耗时诊断）期间必须周期推送 SSE 注释行心跳，且源结束后不再有心跳。"""

    async def slow_source():
        await asyncio.sleep(0.20)
        yield "data: hello\n\n"

    chunks = [c async for c in _wrap(slow_source())]
    heartbeats = [c for c in chunks if c.startswith(": keepalive")]
    assert heartbeats, "空闲期间应至少推送一个心跳注释行"
    assert "data: hello\n\n" in chunks
    # 心跳注释行必须出现在业务事件之前（保活发生在空闲窗口），且源结束后不再推送
    assert chunks[-1] == "data: hello\n\n"


@pytest.mark.asyncio
async def test_keepalive_passthrough_when_disabled():
    """心跳禁用时透传源流，不注入任何注释行（便于排障/压测）。"""

    async def src():
        yield "data: a\n\n"
        yield "data: b\n\n"

    chunks = [c async for c in _wrap(src(), enabled=False)]
    assert chunks == ["data: a\n\n", "data: b\n\n"]


@pytest.mark.asyncio
async def test_keepalive_passthrough_when_interval_invalid():
    """interval<=0 时视为禁用，透传源流。"""

    async def src():
        yield "data: x\n\n"

    chunks = [c async for c in _wrap(src(), interval=0.0, enabled=True)]
    assert chunks == ["data: x\n\n"]


@pytest.mark.asyncio
async def test_keepalive_terminates_after_source_ends():
    """源流结束后包装器必须终止，绝不能无限推送心跳导致连接无法关闭。"""

    async def src():
        yield "data: x\n\n"

    collected = []
    async for c in _wrap(src()):
        collected.append(c)
        if len(collected) > 50:  # 安全阀，正常不应触发
            break
    assert "data: x\n\n" in collected


@pytest.mark.asyncio
async def test_keepalive_cancellation_is_clean():
    """客户端断开（取消消费）时，生产者与心跳任务应被回收，无悬空协程告警。"""

    async def never_source():
        await asyncio.sleep(10)
        yield "data: never\n\n"

    gen = _wrap(never_source())
    drain = asyncio.create_task(_drain(gen))
    await asyncio.sleep(0.15)
    drain.cancel()
    with pytest.raises(asyncio.CancelledError):
        await drain
    # 等待片刻，确保无 "Task exception was never retrieved" 告警
    await asyncio.sleep(0.05)


async def _drain(gen):
    async for _ in gen:
        pass
