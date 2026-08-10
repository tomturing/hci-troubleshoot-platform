"""KBD Vision 异步 Job 轮询的传输错误恢复测试。"""

from unittest.mock import AsyncMock

import httpx
import pytest
from kbd.image_proc import _poll_reanalyze_status, process_images_batch


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


@pytest.mark.asyncio
async def test_empty_batch_is_reported_as_skipped_without_api_setup(caplog):
    """空 Vision 计划应明确说明跳过，而不是打印识图完成 0/0。"""
    with caplog.at_level("DEBUG", logger="kbd.image_proc"):
        result = await process_images_batch([])

    assert result == {"done": 0, "failed": 0, "skipped": 0, "case_results": {}}
    assert "批量识图无需调用" in caplog.text
    assert "批量识图完成" not in caplog.text


@pytest.mark.asyncio
async def test_failed_job_preserves_job_id_and_service_reason(monkeypatch):
    client = AsyncMock(spec=httpx.AsyncClient)
    client.get.return_value = _status_response({
        "status": "failed",
        "total": 3,
        "done": 2,
        "failed": 1,
        "error": "seq=0: Provider 拒绝了图片格式",
    })
    monkeypatch.setattr("kbd.image_proc.asyncio.sleep", AsyncMock())

    with pytest.raises(Exception) as exc_info:
        await _poll_reanalyze_status(11, "job-readable", client, poll_interval=0.01)

    assert getattr(exc_info.value, "job_id", None) == "job-readable"
    assert "Provider 拒绝了图片格式" in str(exc_info.value)


@pytest.mark.asyncio
async def test_unchanged_poll_heartbeats_do_not_repeat_at_info(monkeypatch, caplog):
    client = AsyncMock(spec=httpx.AsyncClient)
    client.get.side_effect = [
        _status_response({"status": "running", "total": 3, "done": 0, "failed": 0}),
        _status_response({"status": "running", "total": 3, "done": 0, "failed": 0}),
        _status_response({"status": "done", "total": 3, "done": 3, "failed": 0}),
    ]
    monkeypatch.setattr("kbd.image_proc.asyncio.sleep", AsyncMock())

    with caplog.at_level("INFO", logger="kbd.image_proc"):
        await _poll_reanalyze_status(11, "job-quiet", client, poll_interval=0.01)

    progress_lines = [line for line in caplog.messages if "识图进度" in line]
    assert len(progress_lines) == 2
