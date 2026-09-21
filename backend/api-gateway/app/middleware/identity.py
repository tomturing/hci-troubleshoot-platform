"""
身份中间件（P0 修复）

为 /api 请求签发/校验服务端身份 Cookie，并解析管理员令牌：
- 无有效 Cookie 时自动签发新的 server-generated client_id（HttpOnly）。
- 解析 `Authorization: Bearer INTERNAL_API_TOKEN` 标记 is_admin。

具体路由是否强制要求身份，由路由上的
`Depends(require_user)` / `Depends(require_admin)` 决定；本中间件只负责
把解析结果放到 request.state 供依赖读取。
"""

import hmac

from shared.security.identity import IDENTITY_COOKIE_NAME, issue_identity, verify_identity
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import settings


class IdentityMiddleware(BaseHTTPMiddleware):
    """签发/校验身份 Cookie，并解析 admin 令牌。"""

    async def dispatch(self, request, call_next):
        request.state.client_id = None
        request.state.is_admin = False
        request.state._issue_cookie = None

        path = request.url.path
        if path.startswith("/api") and not path.startswith(("/api/health", "/api/metrics")):
            cookie = request.cookies.get(IDENTITY_COOKIE_NAME)
            cid = verify_identity(cookie, settings.INTERNAL_API_TOKEN)
            if cid:
                request.state.client_id = cid
            else:
                cid, cookie_value = issue_identity(settings.INTERNAL_API_TOKEN)
                request.state.client_id = cid
                request.state._issue_cookie = cookie_value

            auth = request.headers.get("Authorization", "")
            if auth.startswith("Bearer "):
                token = auth[7:]
                if hmac.compare_digest(token, settings.INTERNAL_API_TOKEN):
                    request.state.is_admin = True

        response = await call_next(request)

        if request.state._issue_cookie:
            response.set_cookie(
                IDENTITY_COOKIE_NAME,
                request.state._issue_cookie,
                httponly=True,
                samesite="lax",
                secure=settings.IDENTITY_COOKIE_SECURE,
                max_age=60 * 60 * 24 * 365,
                path="/",
            )
        return response
