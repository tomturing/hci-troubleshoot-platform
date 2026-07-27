"""exec-result 代理的大请求内存边界回归测试。"""

import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

_svc = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_expect = os.path.normpath(os.path.join(_svc, "app"))
_actual = os.path.normpath(getattr(sys.modules.get("app"), "__path__", [""])[0]) if "app" in sys.modules else ""
if _expect != _actual:
    for _key in list(sys.modules):
        if _key == "app" or _key.startswith("app."):
            del sys.modules[_key]
    if _svc in sys.path:
        sys.path.remove(_svc)
    sys.path.insert(0, _svc)

from app.main import app
from app.routes.conversations import _read_json_body_limited


def test_exec_result_proxy_rejects_body_over_two_mib_before_json_decode():
    client = TestClient(app)
    body = b'{"exec_id":"exec-large","output":"' + b"x" * (2 * 1024 * 1024) + b'","exit_code":0}'

    response = client.post(
        "/api/conversations/00000000-0000-0000-0000-000000008529/exec-result",
        content=body,
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 413
    assert "2 MiB" in response.json()["detail"]


def test_exec_result_proxy_rejects_invalid_json():
    client = TestClient(app)

    response = client.post(
        "/api/conversations/00000000-0000-0000-0000-000000008529/exec-result",
        content=b"not-json",
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "请求 JSON 非法"


def test_exec_result_proxy_rejects_non_object_json():
    client = TestClient(app)

    response = client.post(
        "/api/conversations/00000000-0000-0000-0000-000000008529/exec-result",
        json=["not", "an", "object"],
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "请求 JSON 必须为对象"


@pytest.mark.asyncio
async def test_body_stream_limit_does_not_trust_missing_content_length():
    request = MagicMock()
    request.headers = {}

    async def oversized_stream():
        yield b'x' * 600
        yield b'y' * 500

    request.stream = oversized_stream

    with pytest.raises(HTTPException) as exc_info:
        await _read_json_body_limited(request, 1024)

    assert exc_info.value.status_code == 413


@pytest.mark.asyncio
async def test_body_stream_limit_does_not_trust_forged_content_length():
    request = MagicMock()
    request.headers = {"content-length": "2"}

    async def oversized_stream():
        yield b'x' * 1025

    request.stream = oversized_stream

    with pytest.raises(HTTPException) as exc_info:
        await _read_json_body_limited(request, 1024)

    assert exc_info.value.status_code == 413


@patch("app.routes.conversations.proxy_request", new_callable=AsyncMock)
def test_exec_result_proxy_forwards_small_filtered_payload(mock_proxy):
    upstream = MagicMock()
    upstream.status_code = 200
    upstream.json.return_value = {"ok": True, "exec_id": "exec-small"}
    mock_proxy.return_value = upstream
    client = TestClient(app)

    response = client.post(
        "/api/conversations/00000000-0000-0000-0000-000000008529/exec-result",
        json={
            "exec_id": "exec-small",
            "output": "qemu 9527 /4359974862144/disk.qcow2",
            "exit_code": 0,
            "stdout": "qemu 9527 /4359974862144/disk.qcow2",
        },
    )

    assert response.status_code == 200
    payload = mock_proxy.await_args.kwargs["payload"]
    assert payload["exec_id"] == "exec-small"
    assert len(payload["output"]) < 256
