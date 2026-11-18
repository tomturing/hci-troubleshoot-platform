"""acli 插件阶段二（票据族）网关代理测试。

链路收口后，桥不再有「平台连进来」这条路，改为持短期只读票据反向拉取平台。
网关是这条路的唯一入口，因此本组测试锁四条不可让步的性质：

1. **逐条枚举**：case-service 的三条阶段二路由（ticket / manifest / package）在网关
   各自有显式代理，不用通配前缀兜底（AGENTS.md 硬约束）；
2. 取票族走客户身份：身份由服务端 Cookie 派生后 **HMAC 重签**，浏览器自报身份不转发；
3. 票据族（manifest / package）**只带票据**：既不注入客户身份头，也不转发调用方带来的
   `Authorization`——桥一旦被控，手里也只能是一张短期只读票；
4. 包下载是流式的，且上游 4xx 原样透传：把 404 洗成 200 空体，桥会把截断当下载成功。
"""

from unittest.mock import MagicMock, patch

import httpx
import pytest
from app.config import settings
from app.middleware.identity import IdentityMiddleware
from app.routes.acli_plugins_client import PACKAGE_TIMEOUT_SECONDS, router
from fastapi import FastAPI
from fastapi.testclient import TestClient
from shared.security.signature import verify_client_identity

TICKET = "1790000000." + "a" * 64
CASE_ID = "Q2026111800001"
PLUGIN = "hci_lmt_check"
SPEC_HASH = "b" * 64


def build_client() -> TestClient:
    app = FastAPI()
    app.add_middleware(IdentityMiddleware)
    app.include_router(router)
    return TestClient(app)


class FakeUpstreamClient:
    """同时支持 `async with` 控制面调用与裸实例 `send(stream=True)` 两种用法。

    网关这两条路径的凭据集合**故意不同**（前者带签名身份、后者只带票据），
    所以替身必须把每次调用的 method/url/headers/params 都记下来供断言。
    """

    def __init__(self, response, captured: dict, *, error: Exception | None = None):
        self.response = response
        self.captured = captured
        self.error = error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def aclose(self):
        return None

    def _record(self, method, url, **kwargs):
        self.captured.update(
            method=method,
            url=url,
            headers=kwargs.get("headers") or {},
            params=kwargs.get("params") or {},
            content=kwargs.get("content"),
        )

    async def request(self, method, url, **kwargs):
        if self.error:
            raise self.error
        self._record(method, url, **kwargs)
        return self.response

    async def post(self, url, **kwargs):
        if self.error:
            raise self.error
        self._record("POST", url, **kwargs)
        return self.response

    def build_request(self, method, url, **kwargs):
        self._record(method, url, **kwargs)
        return (method, url)

    async def send(self, request, **kwargs):
        if self.error:
            raise self.error
        self.captured["stream"] = kwargs.get("stream")
        return self.response


def _patch_client(response, captured: dict, *, error: Exception | None = None):
    """把模块里的 httpx.AsyncClient 换成替身工厂，返回可断言调用参数的 patch 对象。"""
    instance = FakeUpstreamClient(response, captured, error=error)
    return instance


def _upstream(*, status_code=200, content=b'{"ok":true}', headers=None, chunks=None):
    response = MagicMock()
    response.status_code = status_code
    response.content = content
    response.headers = headers or {"content-type": "application/json"}

    async def aread():
        return content

    async def aiter_bytes():
        for chunk in (chunks if chunks is not None else [content]):
            yield chunk

    response.aread = aread
    response.aiter_bytes = aiter_bytes
    response.aclose = lambda: aread()
    return response


def _lower(headers: dict) -> dict:
    return {str(key).lower(): value for key, value in headers.items()}


# ── 1. 逐条枚举 ─────────────────────────────────────────────────────────────


def test_gateway_enumerates_exactly_the_three_stage2_routes():
    assert {route.path for route in router.routes} == {
        "/api/acli-plugins/ticket",
        "/api/acli-plugins/manifest",
        "/api/acli-plugins/package/{plugin_name}",
    }


# ── 2. 取票：客户身份重签 ────────────────────────────────────────────────────


def test_ticket_proxy_resigns_server_side_identity():
    client = build_client()
    captured: dict = {}
    response = _upstream(content=b'{"ticket":"t","case_id":"Q"}')
    with patch("app.routes.acli_plugins_client.httpx.AsyncClient") as cls:
        cls.return_value = _patch_client(response, captured)
        out = client.post(
            "/api/acli-plugins/ticket",
            json={"case_id": CASE_ID},
            headers={"X-Client-ID": "attacker-declared", "Authorization": "Bearer leaked-admin-token"},
        )

    assert out.status_code == 200
    # 取票是客户面调用：打到 case-service 的 /api 族（要客户身份），不是 internal 族。
    assert captured["method"] == "POST"
    assert captured["url"].endswith("/api/acli-plugins/ticket")
    headers = _lower(captured["headers"])
    assert verify_client_identity(captured["headers"], settings.INTERNAL_API_TOKEN)
    assert headers["x-client-id"] != "attacker-declared"
    # 浏览器带来的管理令牌一律不转发：取票不需要、也不该具备管理能力。
    assert "authorization" not in headers


def test_ticket_proxy_passes_body_and_upstream_status_through():
    client = build_client()
    captured: dict = {}
    problem = b'{"detail":"Case not found"}'
    with patch("app.routes.acli_plugins_client.httpx.AsyncClient") as cls:
        cls.return_value = _patch_client(_upstream(status_code=404, content=problem), captured)
        out = client.post("/api/acli-plugins/ticket", json={"case_id": "Q0000000000000"})

    assert out.status_code == 404  # 工单不存在不能被网关洗成 200
    assert out.content == problem
    assert captured["content"] == b'{"case_id":"Q0000000000000"}'


# ── 3. 票据族：只带票据 ─────────────────────────────────────────────────────


def test_manifest_proxy_forwards_ticket_and_query_to_internal_route():
    client = build_client()
    captured: dict = {}
    with patch("app.routes.acli_plugins_client.httpx.AsyncClient") as cls:
        cls.return_value = _patch_client(_upstream(content=b'{"manifest_version":"2.0"}'), captured)
        out = client.get(
            "/api/acli-plugins/manifest",
            params={"case_id": CASE_ID},
            headers={"X-Plugin-Ticket": TICKET, "Authorization": "Bearer leaked-admin-token"},
        )

    assert out.status_code == 200
    # 桥没有客户身份也没有管理令牌：票据族必须打到 /internal 的 sync_router。
    assert captured["url"].endswith("/internal/acli-plugins/manifest")
    assert captured["params"] == {"case_id": CASE_ID}
    headers = _lower(captured["headers"])
    assert headers["x-plugin-ticket"] == TICKET
    # 白名单而非黑名单：新加一个头（Cookie/身份）就要在这里显式认领。
    assert set(headers) <= {"accept", "x-plugin-ticket", "traceparent", "tracestate", "x-trace-id"}, headers
    assert "authorization" not in headers
    assert "x-client-id" not in headers
    assert "cookie" not in headers


def test_manifest_proxy_preserves_upstream_401_without_rewrite():
    """票据过期/伪造的 401 必须原样回到桥：网关若降级放行，fail-closed 就形同虚设。"""
    client = build_client()
    captured: dict = {}
    body = b'{"detail":"\xe7\xa5\xa8\xe6\x8d\xae\xe6\x97\xa0\xe6\x95\x88\xef\xbc\x9aticket_expired"}'
    with patch("app.routes.acli_plugins_client.httpx.AsyncClient") as cls:
        cls.return_value = _patch_client(_upstream(status_code=401, content=body), captured)
        out = client.get("/api/acli-plugins/manifest", params={"case_id": CASE_ID})

    assert out.status_code == 401
    assert out.content == body


# ── 4. 包下载：流式 + 长窗口 + 4xx 透传 ──────────────────────────────────────


def test_package_proxy_streams_object_and_keeps_ticket_only_headers():
    client = build_client()
    captured: dict = {}
    chunks = [b"\x7fELF", b"-ish tar", b" payload"]
    upstream = _upstream(
        content=b"".join(chunks),
        headers={"content-type": "application/octet-stream", "content-length": str(sum(map(len, chunks)))},
        chunks=chunks,
    )
    with patch("app.routes.acli_plugins_client.httpx.AsyncClient") as cls:
        cls.return_value = _patch_client(upstream, captured)
        out = client.get(
            f"/api/acli-plugins/package/{PLUGIN}",
            params={"case_id": CASE_ID, "spec_hash": SPEC_HASH},
            headers={"X-Plugin-Ticket": TICKET, "Cookie": "identity=leaked"},
        )

    assert out.status_code == 200
    assert out.content == b"".join(chunks)
    assert captured["url"].endswith(f"/internal/acli-plugins/package/{PLUGIN}")
    # 几十 MB 的包不能在网关整块缓冲：上游必须以 stream 模式取，再逐块转发。
    assert captured["stream"] is True
    headers = _lower(captured["headers"])
    assert headers["x-plugin-ticket"] == TICKET
    assert "cookie" not in headers
    assert "authorization" not in headers


def test_package_proxy_uses_long_read_timeout():
    """单包 20–31 MB、客户上行 0.3–4 MB/s：按 30s 控制面窗口必然掐断下载。"""
    client = build_client()
    captured: dict = {}
    with patch("app.routes.acli_plugins_client.httpx.AsyncClient") as cls:
        cls.return_value = _patch_client(_upstream(chunks=[b"x"]), captured)
        client.get(f"/api/acli-plugins/package/{PLUGIN}", params={"case_id": CASE_ID})

    assert cls.call_args.kwargs["timeout"].read == PACKAGE_TIMEOUT_SECONDS
    # 连接窗口仍要短：服务没起来时不要拖住桥的整段阶段二预算。
    assert cls.call_args.kwargs["timeout"].connect == 10.0
    assert PACKAGE_TIMEOUT_SECONDS >= 600


def test_package_proxy_passes_upstream_404_reason_through():
    """404 的原因码是桥的回退依据（未配置存储 vs 包不存在），不能吞成 200 空体。"""
    client = build_client()
    captured: dict = {}
    body = b'{"detail":{"code":"package_source_undefined"}}'
    with patch("app.routes.acli_plugins_client.httpx.AsyncClient") as cls:
        cls.return_value = _patch_client(_upstream(status_code=404, content=body), captured)
        out = client.get(
            f"/api/acli-plugins/package/{PLUGIN}",
            params={"case_id": CASE_ID, "spec_hash": SPEC_HASH},
            headers={"X-Plugin-Ticket": TICKET},
        )

    assert out.status_code == 404
    assert out.content == body
    assert "package_source_undefined" in out.text


@pytest.mark.parametrize(
    "url",
    ["/api/acli-plugins/ticket", "/api/acli-plugins/manifest", "/api/acli-plugins/package/hci_lmt_check"],
)
def test_stage2_proxies_return_503_when_case_service_is_unreachable(url):
    """上游不可达要显式失败：静默回空清单会让桥以为「平台没有期望插件」而跳过收敛。"""
    client = build_client()
    with patch("app.routes.acli_plugins_client.httpx.AsyncClient") as cls:
        cls.return_value = _patch_client(_upstream(), {}, error=httpx.ConnectError("down"))
        if url.endswith("/ticket"):
            response = client.post(url, json={"case_id": CASE_ID})
        else:
            response = client.get(url, params={"case_id": CASE_ID})
    assert response.status_code == 503, url
