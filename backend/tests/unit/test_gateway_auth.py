"""
网关鉴权接线单测（P0 修复）

覆盖：
- require_user：Cookie 身份优先返回；admin 显式 client_id 覆盖；无身份 → 401
- require_admin：admin 令牌 → True；非 admin → 403
- IdentityMiddleware：无 Cookie 签发并下发；合法 Cookie 复用；篡改 Cookie 重新签发

纯逻辑测试，无 DB / 无下游依赖（fastapi/starlette 由 uvx --with 提供）。
"""

from types import SimpleNamespace

import pytest
from app.config import settings
from app.middleware.identity import IdentityMiddleware
from app.security.gateway_auth import require_admin, require_user
from shared.security.identity import IDENTITY_COOKIE_NAME, issue_identity, verify_identity
from starlette.requests import Request
from starlette.responses import Response

SECRET = settings.INTERNAL_API_TOKEN


async def _noop_app(scope, receive, send):
    """BaseHTTPMiddleware 实例化所需的最小 ASGI app（测试直接调用 dispatch，不会用到）。"""
    return None


def _fake_request(client_id=None, is_admin=False, client_id_q=None):
    req = SimpleNamespace()
    req.state = SimpleNamespace(client_id=client_id, is_admin=is_admin)
    req.query_params = {}
    if client_id_q is not None:
        req.query_params["client_id"] = client_id_q
    return req


@pytest.mark.asyncio
async def test_require_user_returns_cookie_identity():
    req = _fake_request(client_id="user-abc")
    assert await require_user(req) == "user-abc"


@pytest.mark.asyncio
async def test_require_user_admin_override_client_id():
    # 管理员携带显式 client_id 查询参数时，优先使用之（信任 admin 令牌）
    req = _fake_request(client_id="random-cookie-id", is_admin=True, client_id_q="target-client")
    assert await require_user(req) == "target-client"


@pytest.mark.asyncio
async def test_require_user_admin_without_query_falls_back_to_cookie():
    req = _fake_request(client_id="cookie-id", is_admin=True)
    assert await require_user(req) == "cookie-id"


@pytest.mark.asyncio
async def test_require_user_unauthenticated_raises_401():
    req = _fake_request(client_id=None, is_admin=False)
    with pytest.raises(Exception) as exc:
        await require_user(req)
    # FastAPI HTTPException → status_code 401
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_require_admin_allows_token():
    req = _fake_request(is_admin=True)
    # 鉴权通过时依赖返回 None（仅做强制校验）
    assert await require_admin(req) is None


@pytest.mark.asyncio
async def test_require_admin_rejects_non_admin():
    req = _fake_request(is_admin=False)
    with pytest.raises(Exception) as exc:
        await require_admin(req)
    assert exc.value.status_code == 403


# ---------- IdentityMiddleware ----------


def _make_scope(path="/api/cases/", cookie=None, auth=None):
    headers = []
    if cookie is not None:
        headers.append((b"cookie", f"{IDENTITY_COOKIE_NAME}={cookie}".encode()))
    if auth is not None:
        headers.append((b"authorization", auth.encode()))
    return {
        "type": "http",
        "method": "GET",
        "path": path,
        "headers": headers,
        "query_string": b"",
    }


async def _receive():
    return {"type": "http.request", "body": b"", "more_body": False}


async def _call_next(request):
    return Response("ok")


@pytest.mark.asyncio
async def test_middleware_issues_cookie_when_absent():
    req = Request(_make_scope(), _receive)
    resp = await IdentityMiddleware(_noop_app).dispatch(req, _call_next)
    assert req.state.client_id
    assert IDENTITY_COOKIE_NAME in resp.headers.get("set-cookie", "")
    # 校验下发的 Cookie 可被验签
    cookie = resp.headers.get("set-cookie").split(";")[0].split("=", 1)[1]
    assert verify_identity(cookie, SECRET) == req.state.client_id


@pytest.mark.asyncio
async def test_middleware_reuses_valid_cookie():
    cid, cookie = issue_identity(SECRET)
    req = Request(_make_scope(cookie=cookie), _receive)
    resp = await IdentityMiddleware(_noop_app).dispatch(req, _call_next)
    assert req.state.client_id == cid
    # 合法 Cookie 不重新下发
    assert "set-cookie" not in resp.headers


@pytest.mark.asyncio
async def test_middleware_reissues_on_tampered_cookie():
    cid, cookie = issue_identity(SECRET)
    tampered = cookie + "x"  # 破坏签名
    req = Request(_make_scope(cookie=tampered), _receive)
    resp = await IdentityMiddleware(_noop_app).dispatch(req, _call_next)
    # 篡改 Cookie 被拒绝并重新签发，身份与原值不同
    assert req.state.client_id != cid
    assert IDENTITY_COOKIE_NAME in resp.headers.get("set-cookie", "")


@pytest.mark.asyncio
async def test_middleware_marks_admin_on_bearer_token():
    req = Request(
        _make_scope(auth=f"Bearer {SECRET}"),
        _receive,
    )
    await IdentityMiddleware(_noop_app).dispatch(req, _call_next)
    assert req.state.is_admin is True


@pytest.mark.asyncio
async def test_middleware_skips_non_api_paths():
    req = Request(_make_scope(path="/healthz"), _receive)
    await IdentityMiddleware(_noop_app).dispatch(req, _call_next)
    # 非 /api 路径不签发身份
    assert getattr(req.state, "client_id", None) is None
