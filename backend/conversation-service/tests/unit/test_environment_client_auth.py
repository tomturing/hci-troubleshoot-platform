"""conversation-service 直连 case-service 的内部身份签名测试。

真 live E2E 实锤：conversation-service 直连 case-service 取环境上下文（含告警 host）时
不带任何鉴权头，被 case-service 的 `get_client_id`（只认网关 HMAC 签名身份）拒为 401，
导致 env_context 恒空、根因轨 `{{HOST}}` 永久不可解。本组用例钉死「自签 + 可被 case-service
同规格验签」这条修复链路，且无身份时优雅退化（不静默伪造、不阻断）。
"""

import httpx
import pytest
from app.services.environment_client import EnvironmentClient
from shared.security.signature import verify_client_identity

_SECRET = "shared-internal-token"
_CASE_ID = "Q2026102000005"
_OWNER = "live-e2e-smoke"


def _client(handler) -> EnvironmentClient:
    return EnvironmentClient("http://case-service:8001", signing_secret=_SECRET, transport=httpx.MockTransport(handler))


def test_identity_headers_roundtrip_verified_by_case_service() -> None:
    """自签头必须能被 case-service 的验签函数解出正确 client_id（跨服务同密钥契约）。"""
    client = EnvironmentClient("http://case-service:8001", signing_secret=_SECRET)
    headers = client._identity_headers(_OWNER)
    assert verify_client_identity(headers, _SECRET) == _OWNER


def test_identity_headers_empty_without_client_id_or_secret() -> None:
    """无归属身份或无共享密钥时不签名（退化为历史行为，绝不伪造）。"""
    signed = EnvironmentClient("http://case-service:8001", signing_secret=_SECRET)
    assert signed._identity_headers(None) == {}
    unsigned = EnvironmentClient("http://case-service:8001", signing_secret="")
    assert unsigned._identity_headers(_OWNER) == {}


@pytest.mark.asyncio
async def test_get_context_info_sends_verified_identity_headers() -> None:
    captured: list[httpx.Headers] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.headers)
        return httpx.Response(200, json={"env_info": {}, "alert_logs": [], "task_logs": []})

    result = await _client(handler).get_context_info(_CASE_ID, _OWNER)

    assert result is not None
    # 直接对 httpx.Headers（大小写不敏感）验签，避免普通 dict 丢不敏感匹配。
    assert verify_client_identity(captured[0], _SECRET) == _OWNER


@pytest.mark.asyncio
async def test_get_raw_environments_sends_verified_identity_headers() -> None:
    captured: list[httpx.Headers] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.headers)
        return httpx.Response(200, json={"items": [{"env_type": "alert", "env_data": {"alerts": []}}]})

    result = await _client(handler).get_raw_environments(_CASE_ID, _OWNER)

    assert result == {"alert": {"alerts": []}}
    assert verify_client_identity(captured[0], _SECRET) == _OWNER


@pytest.mark.asyncio
async def test_missing_client_id_falls_back_to_unsigned_and_tolerates_401() -> None:
    """未下传身份时不签名；case-service 返回 401 时按超时容忍原则返回 None（不抛异常）。"""
    captured: list[httpx.Headers] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.headers)
        return httpx.Response(401, json={"detail": "身份签名缺失或无效"})

    result = await _client(handler).get_context_info(_CASE_ID)

    assert result is None
    assert captured[0].get("X-Client-ID") is None
