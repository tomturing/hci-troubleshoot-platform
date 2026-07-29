"""KBD Vision 异步 Job 轮询的传输错误恢复测试。"""

from unittest.mock import AsyncMock

import httpx
import pytest
from kbd.image_proc import _poll_reanalyze_status


def _status_response(payload: dict) -> httpx.Response:
    request = httpx.Request("GET", "http://kb-service/status")
    return httpx.Response(200, json=payload, request=request)


@pytest.mark.asyncio
async def test_poll_reuses_same_job_after_transient_disconnect(monkeypatch):
    client = AsyncMock(spec=httpx.AsyncClient)
    client.get.side_effect = [
        httpx.RemoteProtocolError("server disconnected"),
        _status_response({"status": "done", "total": 3, "done": 3, "failed": 0}),
    ]
    monkeypatch.setattr("kbd.image_proc.asyncio.sleep", AsyncMock())
    monkeypatch.setattr("kbd.image_proc.settings.API_MAX_RETRIES", 3)

    result = await _poll_reanalyze_status(
        5,
        "job-stable-id",
        client,
        poll_interval=0.01,
        timeout_total=1.0,
    )

    assert result["success"] is True
    assert result["done"] == 3
    assert client.get.await_count == 2
    assert all(
        call.kwargs["params"] == {"job_id": "job-stable-id"}
        for call in client.get.await_args_list
    )


@pytest.mark.asyncio
async def test_poll_fails_after_bounded_consecutive_transport_errors(monkeypatch):
    client = AsyncMock(spec=httpx.AsyncClient)
    client.get.side_effect = httpx.ConnectError("connection reset")
    monkeypatch.setattr("kbd.image_proc.asyncio.sleep", AsyncMock())
    monkeypatch.setattr("kbd.image_proc.settings.API_MAX_RETRIES", 2)

    with pytest.raises(RuntimeError, match="连续 3 次传输失败"):
        await _poll_reanalyze_status(
            5,
            "job-stable-id",
            client,
            poll_interval=0.01,
            timeout_total=1.0,
        )

    assert client.get.await_count == 3
