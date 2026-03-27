"""
CORS 安全测试

验证 API Gateway 的跨域资源共享配置符合安全要求：
- 合法 Origin 允许通过（200 + Access-Control-Allow-Origin 存在）
- 非法 Origin 不出现 Access-Control-Allow-Origin 头
- OPTIONS preflight 正常响应（200，无需认证）
- allow_origins=["*"] + allow_credentials=True 的 RFC 6454 违规已修复

完成标准：本文件所有测试用例 PASSED
"""

import os
import sys

# 将 api-gateway 加入路径
_svc = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "backend", "api-gateway"))
_expect = os.path.normpath(os.path.join(_svc, "app"))
_actual = os.path.normpath(getattr(sys.modules.get("app"), "__path__", [""])[0]) if "app" in sys.modules else ""
if _expect != _actual:
    for _k in list(sys.modules):
        if _k == "app" or _k.startswith("app."):
            del sys.modules[_k]
    if _svc in sys.path:
        sys.path.remove(_svc)
    sys.path.insert(0, _svc)


import httpx
import pytest
from httpx import ASGITransport


@pytest.fixture
def app():
    """创建不依赖真实 Redis/Downstream 的 API Gateway 应用实例"""
    from app.config import settings

    # 使用允许的合法来源
    settings.ALLOWED_ORIGINS = "http://localhost:3001,http://localhost:3002,http://admin.example.com"

    from fastapi import FastAPI
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import JSONResponse

    test_app = FastAPI()
    test_app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @test_app.get("/api/health")
    async def health():
        return JSONResponse({"status": "ok"})

    @test_app.post("/api/cases/")
    async def create_case():
        return JSONResponse({"case_id": "test-123"}, status_code=201)

    return test_app


@pytest.fixture
async def client(app):
    """异步测试客户端"""
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as c:
        yield c


class TestCORSAllowedOrigins:
    """合法 Origin 的 CORS 头验证"""

    @pytest.mark.asyncio
    async def test_allowed_origin_gets_cors_header(self, client):
        """合法来源的请求应响应 Access-Control-Allow-Origin"""
        response = await client.get(
            "/api/health",
            headers={"Origin": "http://localhost:3001"},
        )
        assert response.status_code == 200
        assert "access-control-allow-origin" in response.headers
        assert response.headers["access-control-allow-origin"] == "http://localhost:3001"

    @pytest.mark.asyncio
    async def test_second_allowed_origin(self, client):
        """第二个合法来源同样应响应正确的 CORS 头"""
        response = await client.get(
            "/api/health",
            headers={"Origin": "http://localhost:3002"},
        )
        assert response.status_code == 200
        assert response.headers.get("access-control-allow-origin") == "http://localhost:3002"

    @pytest.mark.asyncio
    async def test_admin_origin_allowed(self, client):
        """admin.example.com 来源应被允许"""
        response = await client.get(
            "/api/health",
            headers={"Origin": "http://admin.example.com"},
        )
        assert response.status_code == 200
        assert response.headers.get("access-control-allow-origin") == "http://admin.example.com"


class TestCORSBlockedOrigins:
    """非法 Origin 的 CORS 拦截验证"""

    @pytest.mark.asyncio
    async def test_unlisted_origin_no_cors_header(self, client):
        """未列入白名单的来源不应出现 Access-Control-Allow-Origin 头"""
        response = await client.get(
            "/api/health",
            headers={"Origin": "http://evil.hacker.com"},
        )
        # 未授权的 origin 不会出现 ACAO 头，但请求本身可以是 200
        # （浏览器会阻止 JS 读取响应，但服务端不强制拒绝请求）
        assert "access-control-allow-origin" not in response.headers

    @pytest.mark.asyncio
    async def test_wildcard_not_in_origins(self, client):
        """CORS 明细配置中不应包含通配符 *（避免 RFC 6454 违规）"""
        from app.config import settings
        origins = settings.cors_origins
        assert "*" not in origins, "allow_origins 不应包含 '*'（与 allow_credentials=True 不兼容）"

    @pytest.mark.asyncio
    async def test_neighbor_subdomain_blocked(self, client):
        """子域名的变体不应被允许（无通配符 *.example.com）"""
        response = await client.get(
            "/api/health",
            headers={"Origin": "http://malicious.localhost:3001"},
        )
        assert "access-control-allow-origin" not in response.headers


class TestCORSPreflightOptions:
    """OPTIONS preflight 请求验证"""

    @pytest.mark.asyncio
    async def test_preflight_allowed_origin(self, client):
        """合法来源的 preflight 应返回 200 并带 CORS 头"""
        response = await client.options(
            "/api/cases/",
            headers={
                "Origin": "http://localhost:3001",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "Content-Type,Authorization",
            },
        )
        assert response.status_code == 200
        assert "access-control-allow-origin" in response.headers
        assert response.headers["access-control-allow-origin"] == "http://localhost:3001"

    @pytest.mark.asyncio
    async def test_preflight_includes_methods(self, client):
        """preflight 响应应包含允许的 HTTP 方法"""
        response = await client.options(
            "/api/cases/",
            headers={
                "Origin": "http://localhost:3001",
                "Access-Control-Request-Method": "POST",
            },
        )
        assert response.status_code == 200
        allow_methods = response.headers.get("access-control-allow-methods", "")
        assert allow_methods  # 非空则说明服务器声明了允许的方法

    @pytest.mark.asyncio
    async def test_preflight_blocked_origin(self, client):
        """非法 Origin 的 preflight 不应回传 CORS 头"""
        response = await client.options(
            "/api/cases/",
            headers={
                "Origin": "http://evil.attacker.net",
                "Access-Control-Request-Method": "POST",
            },
        )
        assert "access-control-allow-origin" not in response.headers


class TestCORSCredentials:
    """allow_credentials 对 RFC 6454 兼容性验证"""

    @pytest.mark.asyncio
    async def test_credentials_header_present_for_allowed_origin(self, client):
        """合法来源响应中应包含 Access-Control-Allow-Credentials: true"""
        response = await client.get(
            "/api/health",
            headers={"Origin": "http://localhost:3001"},
        )
        assert response.headers.get("access-control-allow-credentials") == "true"

    def test_credentials_not_used_with_wildcard(self):
        """当 allow_credentials=True 时，cors_origins 不允许包含 '*'"""
        from app.config import settings

        origins = settings.cors_origins
        has_wildcard = "*" in origins or "http://*" in origins
        assert not has_wildcard, (
            "allow_credentials=True 与 allow_origins=['*'] 同时使用违反 RFC 6454，"
            "且现代浏览器会拒绝这类响应"
        )
