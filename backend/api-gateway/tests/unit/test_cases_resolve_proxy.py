"""`POST /api/cases/resolve` 网关代理测试。

日志收集入口用该接口"按工单号解析/建档"；若网关不注册，前端会拿到
`{"detail":"Not Found"}`（#1205 网关缺路由的同类问题），因此单独锁定：
路径转发、请求体透传、客户身份 HMAC 重签。
"""

from unittest.mock import AsyncMock, MagicMock, patch

from app.config import settings
from app.middleware.identity import IdentityMiddleware
from app.routes.cases import router
from fastapi import FastAPI
from fastapi.testclient import TestClient
from shared.security.signature import verify_client_identity


def build_client() -> TestClient:
    app = FastAPI()
    app.add_middleware(IdentityMiddleware)
    app.include_router(router)
    return TestClient(app)


def test_resolve_case_proxies_body_and_resigns_identity():
    client = build_client()
    captured: dict = {}
    upstream = MagicMock()
    upstream.status_code = 200
    upstream.json.return_value = {"case_id": "Q2026101401684", "created": True}

    async def fake_request(method, url, **kwargs):
        captured.update(method=method, url=url, **kwargs)
        return upstream

    with patch("app.routes.cases.httpx.AsyncClient") as mock_client_cls:
        instance = AsyncMock()
        instance.request = fake_request
        ctx = AsyncMock()
        ctx.__aenter__.return_value = instance
        ctx.__aexit__.return_value = False
        mock_client_cls.return_value = ctx

        response = client.post("/api/cases/resolve", json={"case_id": "Q2026101401684"})

    assert response.status_code == 200
    assert response.json() == {"case_id": "Q2026101401684", "created": True}
    assert captured["method"] == "POST"
    assert captured["url"].endswith("/api/cases/resolve")
    assert captured["json"] == {"case_id": "Q2026101401684"}

    headers = captured["headers"]
    signed_client_id = verify_client_identity(headers, settings.INTERNAL_API_TOKEN)
    assert signed_client_id
    assert headers["X-Client-ID"] == signed_client_id
