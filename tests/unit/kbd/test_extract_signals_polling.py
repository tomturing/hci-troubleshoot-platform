"""KBD Signal 异步 Job 轮询的传输错误恢复测试。"""

from unittest.mock import AsyncMock

import httpx
import pytest
from kbd.extract_signals import _poll_extract_status, extract_signals_batch


def _status_response(payload: dict) -> httpx.Response:
    request = httpx.Request("GET", "http://kb-service/status")
    return httpx.Response(200, json=payload, request=request)


@pytest.mark.asyncio
async def test_signal_poll_reuses_job_after_transient_disconnect(monkeypatch):
    client = AsyncMock(spec=httpx.AsyncClient)
    client.get.side_effect = [
        httpx.RemoteProtocolError("server disconnected"),
        _status_response({
            "status": "done",
            "result": {"signals_count": 4, "rejected_count": 1},
        }),
    ]
    monkeypatch.setattr("kbd.extract_signals.asyncio.sleep", AsyncMock())
    monkeypatch.setattr("kbd.extract_signals.settings.API_MAX_RETRIES", 3)

    result = await _poll_extract_status(
        5,
        "signal-job-id",
        client,
        poll_interval=0.01,
        timeout_total=1.0,
    )

    assert result == {
        "success": True,
        "kbd_id": 5,
        "signals_count": 4,
        "rejected_count": 1,
        "message": "异步信号抽取完成",
    }
    assert client.get.await_count == 2
    assert all(
        call.kwargs["params"] == {"job_id": "signal-job-id"}
        for call in client.get.await_args_list
    )


@pytest.mark.asyncio
async def test_signal_poll_fails_after_bounded_transport_errors(monkeypatch):
    client = AsyncMock(spec=httpx.AsyncClient)
    client.get.side_effect = httpx.ConnectError("connection reset")
    monkeypatch.setattr("kbd.extract_signals.asyncio.sleep", AsyncMock())
    monkeypatch.setattr("kbd.extract_signals.settings.API_MAX_RETRIES", 2)

    with pytest.raises(RuntimeError, match="连续 3 次传输失败"):
        await _poll_extract_status(
            5,
            "signal-job-id",
            client,
            poll_interval=0.01,
            timeout_total=1.0,
        )

    assert client.get.await_count == 3


@pytest.mark.asyncio
async def test_batch_counts_zero_signal_success_as_needs_review(monkeypatch):
    pool = AsyncMock()
    pool.fetchval.return_value = 1
    results = iter([
        {"success": True, "signals_count": 2, "rejected_count": 0},
        {"success": True, "signals_count": 0, "rejected_count": 3},
    ])

    async def fake_extract(*_args, **_kwargs):
        return next(results)

    monkeypatch.setattr("kbd.extract_signals._call_extract_api", fake_extract)
    stats = await extract_signals_batch(["ok", "empty"], pool)

    assert stats == {"done": 1, "failed": 0, "skipped": 0, "needs_review": 1}
