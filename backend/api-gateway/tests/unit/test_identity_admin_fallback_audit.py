"""共享令牌→admin 回退审计埋点单测（PR-D 关后门实测判据）。

背景：`IdentityMiddleware` 在 `AUTHN_ENFORCE_ADMIN=false` 时，把携带
`INTERNAL_API_TOKEN` 的请求回退赋予 admin 身份。关该后门前的实测判据是
"是否仍有合法调用方依赖此回退"，故每次命中必须计数 + 写审计日志（旁路观测，
**绝不改变鉴权行为**）。

锁定三点：
1. 回退命中：`is_admin` 置 True（行为不变）+ counter 自增 + 审计日志字段齐全且不含令牌明文。
2. 开关打开（`AUTHN_ENFORCE_ADMIN=true`）：不再命中，counter 不增（关后门后的预期）。
3. 非共享令牌（随机 Bearer）：不命中、不计数。
"""

import asyncio
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

_svc = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _svc not in sys.path:
    sys.path.insert(0, _svc)

from app.config import settings
from app.middleware.identity import IdentityMiddleware
from shared.observability.metrics import AUTHZ_SHARED_TOKEN_ADMIN_TOTAL


def _counter(method: str) -> int:
    """读取指定 method 的当前计数值。"""
    return int(AUTHZ_SHARED_TOKEN_ADMIN_TOTAL.labels(method=method)._value.get())


def _make_request(authorization: str, *, method: str = "GET", user_agent: str = "curl/8.0"):
    """构造仅供 dispatch 使用的最小 request 替身。

    刻意使用非 `/api` 路径，避开与回退逻辑无关的 Cookie 身份签发分支，聚焦共享令牌回退本身。
    """
    req = SimpleNamespace()
    req.method = method
    req.state = SimpleNamespace(client_id=None, is_admin=False, auth=None)
    req.app = SimpleNamespace(state=SimpleNamespace(jwt_verifier=None))
    req.headers = {"Authorization": authorization, "User-Agent": user_agent}
    req.url = SimpleNamespace(path="/admin-fallback-probe")
    req.cookies = {}
    req.client = SimpleNamespace(host="10.0.0.9")
    return req


async def _call_next(_request):
    return MagicMock()


def test_shared_token_fallback_grants_admin_and_audits():
    """回退命中：行为不变（is_admin=True）+ 计数 +1 + 审计日志字段齐全且不含令牌。"""
    middleware = IdentityMiddleware(app=_call_next)
    token = settings.INTERNAL_API_TOKEN
    before = _counter("GET")
    req = _make_request(f"Bearer {token}")

    with patch("app.middleware.identity.logger") as mock_logger:
        asyncio.run(middleware.dispatch(req, _call_next))

    assert req.state.is_admin is True, "回退必须仍赋予 admin（埋点不得改变鉴权行为）"
    assert _counter("GET") == before + 1, "命中必须使 counter 自增"
    mock_logger.warning.assert_called_once()
    kwargs = mock_logger.warning.call_args.kwargs
    assert mock_logger.warning.call_args.args[0] == "authz_shared_token_admin_fallback"
    assert kwargs["path"] == "/admin-fallback-probe"
    assert kwargs["method"] == "GET"
    assert kwargs["client_ip"] == "10.0.0.9"
    assert kwargs["user_agent"] == "curl/8.0"
    assert kwargs["trace_id"], "审计日志必须携带唯一调用链 trace_id"
    # 安全红线：日志调用参数中绝不含共享令牌明文
    if token:
        assert token not in str(mock_logger.warning.call_args)


def test_shared_token_not_audited_when_enforce_enabled():
    """AUTHN_ENFORCE_ADMIN=true 时回退关闭：不赋予 admin、不计数（关后门后的预期行为）。"""
    middleware = IdentityMiddleware(app=_call_next)
    before = _counter("POST")
    req = _make_request(f"Bearer {settings.INTERNAL_API_TOKEN}", method="POST")

    with patch.object(settings, "AUTHN_ENFORCE_ADMIN", True), patch("app.middleware.identity.logger") as mock_logger:
        asyncio.run(middleware.dispatch(req, _call_next))

    assert req.state.is_admin is False
    assert _counter("POST") == before
    mock_logger.warning.assert_not_called()


def test_non_shared_token_not_audited():
    """非共享令牌（随机 Bearer，且无 JWT 验签器）：不命中、不计数。"""
    middleware = IdentityMiddleware(app=_call_next)
    before = _counter("GET")
    req = _make_request("Bearer some-random-not-shared-token")

    with patch("app.middleware.identity.logger") as mock_logger:
        asyncio.run(middleware.dispatch(req, _call_next))

    assert req.state.is_admin is False
    assert _counter("GET") == before
    mock_logger.warning.assert_not_called()
