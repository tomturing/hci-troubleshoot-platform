"""IdentityMiddleware 滑动续期回归测试。

背景（工单 Q2026101330905「上游服务暂时不可用」误报根因）：
旧实现下，合法身份 Cookie 验签成功后不刷新内嵌时间戳，一旦超过旧 300s 硬窗口，
下一次请求即被判定失效 → 网关静默铸造全新 client_id → 工单仍归属旧 id →
conversation-service 归属校验 403「无权访问此会话」→ 网关兜底成「上游服务暂时不可用」。

本测试锁定修复后的行为：
- 合法 Cookie：身份保持不变，且回写刷新时间戳的 Set-Cookie（滑动续期）。
- 时间戳已超旧 300s 但在最大寿命内的 Cookie：仍复用同一身份，绝不轮换。
"""

import hashlib
import hmac
import os
import sys
import time
from types import SimpleNamespace

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

import pytest  # noqa: E402
from app.config import settings  # noqa: E402
from app.middleware.identity import IdentityMiddleware  # noqa: E402
from shared.security.identity import (  # noqa: E402
    IDENTITY_COOKIE_NAME,
    issue_identity,
    verify_identity,
)
from starlette.requests import Request  # noqa: E402
from starlette.responses import Response  # noqa: E402

SECRET = settings.INTERNAL_API_TOKEN


def _scope_with_app(cookie: str | None, path: str = "/api/conversations/x") -> dict:
    headers = []
    if cookie is not None:
        headers.append((b"cookie", f"{IDENTITY_COOKIE_NAME}={cookie}".encode()))
    # 关键：scope 必须含 "app"，否则中间件访问 request.app.state.jwt_verifier 会 KeyError
    fake_app = SimpleNamespace(state=SimpleNamespace(jwt_verifier=None))
    return {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "path": path,
        "raw_path": path.encode(),
        "headers": headers,
        "query_string": b"",
        "app": fake_app,
        "client": ("10.0.0.1", 1234),
        "server": ("testserver", 80),
        "scheme": "http",
    }


async def _receive():
    return {"type": "http.request", "body": b"", "more_body": False}


async def _call_next(request):
    return Response("ok")


async def _noop_app(scope, receive, send):
    return None


def _signed_cookie_with_ts(client_id: str, ts: int) -> str:
    digest = hmac.new(SECRET.encode(), f"{ts}:{client_id}".encode(), hashlib.sha256).hexdigest()
    return f"{ts}.{client_id}.{digest}"


@pytest.mark.asyncio
async def test_valid_cookie_identity_preserved_and_slid():
    """合法 Cookie：身份不变，且回写刷新时间戳的 Set-Cookie。"""
    cid, cookie = issue_identity(SECRET)
    req = Request(_scope_with_app(cookie), _receive)
    resp = await IdentityMiddleware(_noop_app).dispatch(req, _call_next)

    assert req.state.client_id == cid
    set_cookie = resp.headers.get("set-cookie", "")
    assert IDENTITY_COOKIE_NAME in set_cookie
    refreshed = set_cookie.split(";")[0].split("=", 1)[1]
    # 续期后仍是同一身份
    assert verify_identity(refreshed, SECRET) == cid


@pytest.mark.asyncio
async def test_stale_but_within_max_age_does_not_rotate():
    """时间戳超过旧 300s 但在最大寿命内：绝不轮换身份（直接复现回归场景）。"""
    cid = "a" * 32
    stale = _signed_cookie_with_ts(cid, int(time.time()) - 900)  # 15 分钟前
    req = Request(_scope_with_app(stale), _receive)
    resp = await IdentityMiddleware(_noop_app).dispatch(req, _call_next)

    # 身份保持不变（旧实现此处会被判定超窗并铸造新 id）
    assert req.state.client_id == cid
    refreshed = resp.headers.get("set-cookie", "").split(";")[0].split("=", 1)[1]
    assert verify_identity(refreshed, SECRET) == cid
