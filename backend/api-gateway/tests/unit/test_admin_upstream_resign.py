"""管理员上游请求必须用网关自身身份重签，不得透传客户端凭证。

背景：管理员登录（auth-service JWT）后，网关若把客户端的 Authorization
原样转发给下游（case-service / kb-service），下游以 INTERNAL_API_TOKEN
校验管理员端点 → 403「管理员凭证无效」，表现为"登录之后管理台反而打不开"。
"""

import os
import sys
from types import SimpleNamespace

_svc = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _svc not in sys.path:
    sys.path.insert(0, _svc)

from app.config import settings
from app.routes import cases, kb

CLIENT_JWT = "Bearer eyJhbGciOiJSUzI1NiJ9.eyJyZWFsbSI6ImFkbWluIn0.sig"


def _admin_request() -> SimpleNamespace:
    return SimpleNamespace(
        headers={"Authorization": CLIENT_JWT},
        state=SimpleNamespace(is_admin=True, client_id="u-1"),
    )


def test_cases_admin_headers_resign_with_internal_token() -> None:
    """cases 管理端：下游拿到的是网关内部令牌，不是客户端 JWT。"""
    headers = cases._admin_headers(_admin_request())
    assert headers["Authorization"] == f"Bearer {settings.INTERNAL_API_TOKEN}"
    assert "eyJ" not in headers["Authorization"]


def test_kb_admin_headers_resign_with_internal_token() -> None:
    """kb 管理端：重签头同样使用网关内部令牌。"""
    headers = kb._internal_auth_headers()
    assert headers["Authorization"] == f"Bearer {settings.INTERNAL_API_TOKEN}"
    assert "eyJ" not in headers["Authorization"]
