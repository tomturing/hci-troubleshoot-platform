"""simulations.py 内部 _case_request 单元测试：修复 case-service 401 回归。

回归背景：#1116/#1118 收紧 case-service 鉴权后，其客户侧路由 POST / 与 GET /{case_id}
用 get_client_id 校验 HMAC 签名身份头；simulations.py 的 _case_request 内部调用
case-service 时必须以 hci-sim-admin 的服务间身份重签，否则返回 401 并透传给前端。
"""

import json
import os
import sys

_svc = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _svc not in sys.path:
    sys.path.insert(0, _svc)

from unittest.mock import AsyncMock, patch

import pytest
from app.config import settings
from app.routes.simulations import HCI_SIM_ADMIN_CLIENT_ID, _case_request
from shared.security.signature import verify_client_identity


@pytest.mark.asyncio
async def test_case_request_injects_signed_hci_sim_admin_identity():
    """_case_request 必须注入可被 case-service 验签的 X-Client-ID=hci-sim-admin 身份头。"""
    captured: dict = {}

    class _Resp:
        status_code = 200

        def json(self):
            return {"case_id": "Q1"}

    async def _fake_request(method, url, **kwargs):
        captured["headers"] = kwargs.get("headers", {})
        return _Resp()

    with patch("app.routes.simulations.httpx.AsyncClient") as MockClient:
        instance = AsyncMock()
        instance.request = _fake_request
        ctx = AsyncMock()
        ctx.__aenter__.return_value = instance
        ctx.__aexit__.return_value = False
        MockClient.return_value = ctx

        resp = await _case_request("GET", "/Q1")

    assert resp.status_code == 200
    assert json.loads(resp.body) == {"case_id": "Q1"}
    headers = captured["headers"]
    assert headers.get("X-Client-ID") == HCI_SIM_ADMIN_CLIENT_ID
    assert "X-Client-Signature" in headers
    # 签名必须能被 case-service 的 verify_client_identity 用 INTERNAL_API_TOKEN 校验通过
    assert verify_client_identity(headers, settings.INTERNAL_API_TOKEN) == HCI_SIM_ADMIN_CLIENT_ID


@pytest.mark.asyncio
async def test_case_request_passes_through_case_service_error():
    """_case_request 必须原样透传 case-service 的 4xx/5xx（不吞错、不改写状态码）。"""

    class _Resp:
        status_code = 401

        def json(self):
            return {"detail": "身份签名缺失或无效"}

    async def _fake_request(method, url, **kwargs):
        return _Resp()

    with patch("app.routes.simulations.httpx.AsyncClient") as MockClient:
        instance = AsyncMock()
        instance.request = _fake_request
        ctx = AsyncMock()
        ctx.__aenter__.return_value = instance
        ctx.__aexit__.return_value = False
        MockClient.return_value = ctx

        resp = await _case_request("GET", "/Q1")

    assert resp.status_code == 401
    assert json.loads(resp.body)["detail"] == "身份签名缺失或无效"
