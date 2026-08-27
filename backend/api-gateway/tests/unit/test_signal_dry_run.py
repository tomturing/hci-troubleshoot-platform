"""Signal 试运行 Gateway 信任边界测试。"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.routes.signal_dry_run import _resolve_authoritative_dataset
from fastapi import FastAPI, Request


def _request() -> Request:
    app = FastAPI()
    return Request({"type": "http", "method": "POST", "path": "/", "headers": [], "app": app, "client": ("test", 1), "server": ("test", 80), "scheme": "http"})


@pytest.mark.asyncio
@patch("app.routes.signal_dry_run.httpx.AsyncClient")
async def test_authoritative_dataset_overwrites_browser_payload(client_cls):
    client = AsyncMock()
    client_cls.return_value.__aenter__.return_value = client
    response = MagicMock(status_code=200)
    response.json.return_value = {"datasets": [{"dataset_id": "asset-1", "source_ref": "sha256:" + "a" * 64 + ":asset-1", "payload": "trusted stdout"}]}
    client.get.return_value = response
    payload = {"unit_ref": {"signal_id": "sig_1"}, "dataset": {"source_type": "fixture", "source_ref": "sha256:" + "a" * 64 + ":asset-1", "payload": "attacker"}}

    resolved = await _resolve_authoritative_dataset(payload, _request())

    assert resolved["dataset"]["payload"] == "trusted stdout"
    assert resolved["dataset"]["dataset_id"] == "asset-1"


@pytest.mark.asyncio
async def test_authoritative_dataset_requires_bundle_reference():
    payload = {"unit_ref": {"signal_id": "sig_1"}, "dataset": {"source_type": "replay", "source_ref": "browser-id", "payload": "attacker"}}

    with pytest.raises(Exception, match="source_ref"):
        await _resolve_authoritative_dataset(payload, _request())
