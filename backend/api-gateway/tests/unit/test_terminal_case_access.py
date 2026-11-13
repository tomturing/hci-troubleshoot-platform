"""终端数据的工单访问判定（verify_case_owner）行为测试。

背景（2026-11-13 Q2026111301229）：终端录像与桥日志此前只按 ``case.client_id``
判定归属。同一个人换浏览器后采集能跑、录像回放却必然 403，且工单本身是日志入口
自建的壳工单（零会话数据）。修复后判定收敛到 case-service 的 ``/data-access``：
归属，或「壳工单 + 本人采集任务」。

本组测试锁定网关侧只做转译、不自行扩权：
  - 下游 data_access=false → 403；
  - 下游 404 → 403（不泄露工单是否存在）；
  - 下游不可达 → 503（不能当成"无权限"让用户去换号）。
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest
from app.routes.terminal import verify_case_owner
from fastapi import HTTPException


def _client(response=None, *, error: Exception | None = None):
    """构造 httpx.AsyncClient 的异步上下文替身。"""
    client = MagicMock()
    if error is not None:
        client.get = AsyncMockSideEffect(error)
    else:
        client.get = AsyncMockReturn(response)
    ctx = MagicMock()
    ctx.__aenter__.return_value = client
    ctx.__aexit__.return_value = False
    factory = MagicMock(return_value=ctx)
    return factory


class AsyncMockReturn:
    def __init__(self, response):
        self._response = response
        self.calls: list[str] = []

    async def __call__(self, url, headers=None):
        self.calls.append(url)
        return self._response


class AsyncMockSideEffect:
    def __init__(self, error):
        self._error = error

    async def __call__(self, url, headers=None):
        raise self._error


def _response(payload=None, status_code: int = 200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = payload or {}
    return resp


pytestmark = pytest.mark.asyncio


class TestVerifyCaseOwner:
    async def test_allows_when_downstream_grants(self):
        factory = _client(_response({"data_access": True, "owned": True}))
        with patch("app.routes.terminal.httpx.AsyncClient", factory):
            await verify_case_owner("Q1", "cust-A")

    async def test_uses_data_access_endpoint(self):
        factory = _client(_response({"data_access": True}))
        with patch("app.routes.terminal.httpx.AsyncClient", factory):
            await verify_case_owner("Q1", "cust-A")

        client = factory.return_value.__aenter__.return_value
        assert client.get.calls[0].endswith("/api/cases/Q1/data-access")

    async def test_rejects_when_downstream_denies(self):
        factory = _client(_response({"data_access": False, "owned": False}))
        with patch("app.routes.terminal.httpx.AsyncClient", factory):
            with pytest.raises(HTTPException) as exc_info:
                await verify_case_owner("Q1", "cust-A")

        assert exc_info.value.status_code == 403

    async def test_missing_case_maps_to_403_not_404(self):
        """存在性也不该泄露给非归属身份。"""
        factory = _client(_response(status_code=404))
        with patch("app.routes.terminal.httpx.AsyncClient", factory):
            with pytest.raises(HTTPException) as exc_info:
                await verify_case_owner("Q1", "cust-A")

        assert exc_info.value.status_code == 403

    async def test_unreachable_case_service_is_503(self):
        factory = _client(error=httpx.ConnectError("boom"))
        with patch("app.routes.terminal.httpx.AsyncClient", factory):
            with pytest.raises(HTTPException) as exc_info:
                await verify_case_owner("Q1", "cust-A")

        assert exc_info.value.status_code == 503
