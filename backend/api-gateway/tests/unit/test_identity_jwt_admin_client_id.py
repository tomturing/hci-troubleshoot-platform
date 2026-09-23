"""admin JWT 身份必须携带 client_id。

背景：`IdentityMiddleware` 在 admin JWT 验签成功后会跳过 Cookie 匿名身份签发，
若 `_apply_actor` 不同步写入 client_id，下游以 client_id 为必填身份的路由
（如 `/api/diagnosis-scenarios`）会对管理台请求返回 401「缺少服务端签发身份」——
表现为"不带凭证反而正常、登录之后反而打不开"，只在真实链路暴露
（单测 mock 请求、构建期 import 冒烟都覆盖不到）。

这里直接锁定 `_apply_actor` 的行为，防止回归。
"""

import os
import sys
from types import SimpleNamespace

_svc = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _svc not in sys.path:
    sys.path.insert(0, _svc)

from app.config import settings
from app.middleware.identity import IdentityMiddleware
from app.security.jwt_verify import GatewayActor

ADMIN_USER_ID = "00000000-0000-0000-0000-000000000001"


def _request() -> SimpleNamespace:
    return SimpleNamespace(state=SimpleNamespace(client_id=None, is_admin=False, auth=None))


def test_admin_jwt_actor_sets_client_id() -> None:
    """admin JWT 身份必须写入 client_id，否则下游必填身份路由 401。"""
    actor = GatewayActor(
        user_id=ADMIN_USER_ID,
        realm="admin",
        roles=frozenset({"platform_admin"}),
        audience=settings.AUTH_JWT_AUD_ADMIN,
    )
    request = _request()
    IdentityMiddleware._apply_actor(request, actor)
    assert request.state.is_admin is True
    assert request.state.client_id == ADMIN_USER_ID


def test_customer_jwt_actor_does_not_set_client_id() -> None:
    """customer realm 本期（阶段2 才启用）不覆盖 Cookie 身份。"""
    actor = GatewayActor(
        user_id=ADMIN_USER_ID,
        realm="customer",
        roles=frozenset({"customer"}),
        audience="hci-customer",
    )
    request = _request()
    IdentityMiddleware._apply_actor(request, actor)
    assert request.state.is_admin is False
    assert request.state.client_id is None
