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
    """签发/校验身份 Cookie，并验证 auth-service 签发的 JWT（阶段1 admin 强认证）。

    身份来源优先级：
    1. auth-service 签发的 JWT（Bearer，非共享令牌）→ 按 realm 设置 is_admin / auth
    2. 现有 Cookie 身份（匿名/客户路径，保持兼容）
    3. INTERNAL_API_TOKEN 共享令牌（AUTHN_ENFORCE_ADMIN 关闭时仍作 admin 兼容）
    """

    async def dispatch(self, request, call_next):
        request.state.client_id = None
        request.state.is_admin = False
        request.state.auth = None  # GatewayActor | None（来自 JWT 验签）
        request.state._issue_cookie = None

        verifier = getattr(request.app.state, "jwt_verifier", None)
        auth = request.headers.get("Authorization", "")

        # 阶段1：优先用 auth-service 签发的 JWT 认证（替换共享令牌作为 admin 身份）
        if verifier is not None and auth.startswith("Bearer "):
            token = auth[7:]
            if not hmac.compare_digest(token, settings.INTERNAL_API_TOKEN):
                actor = await self._try_jwt(verifier, request, token)
                if actor is not None:
                    self._apply_actor(request, actor)

        # 现有 Cookie 身份（匿名/客户路径；若已被 JWT 设为 admin 则跳过）
        if request.state.client_id is None and not request.state.is_admin:
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

        # AUTHN_ENFORCE_ADMIN：开启后共享令牌不再赋予 admin（必须 JWT 登录）
        if (
            not settings.AUTHN_ENFORCE_ADMIN
            and auth.startswith("Bearer ")
            and hmac.compare_digest(auth[7:], settings.INTERNAL_API_TOKEN)
        ):
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

    @staticmethod
    async def _try_jwt(verifier, request, token: str):
        """尝试 JWT 验签；失败返回 None（回退到 Cookie / 共享令牌路径）。"""

        try:
            return await verifier.verify(request)
        except ValueError:
            return None

    @staticmethod
    def _apply_actor(request, actor) -> None:
        """按 realm 把 JWT 身份写入 request.state。"""

        if actor.realm == "admin" and actor.audience == settings.AUTH_JWT_AUD_ADMIN:
            request.state.is_admin = True
            request.state.auth = actor
            request.state.client_id = actor.user_id
            # admin 身份也要有 client_id：下游大量路由（如 /api/diagnosis-scenarios）
            # 以 request.state.client_id 为必填身份，缺失即 401「缺少服务端签发身份」。
            # 而上面的 Cookie 匿名身份分支在 is_admin 为真时被跳过，
            # 若此处不补 client_id，一旦前端改为携带 JWT（含开启 AUTHN_ENFORCE_ADMIN 后），
            # 管理台这些接口会整体 401。这里用 JWT 的 sub（管理员 user_id）作为 client_id。
        elif actor.realm == "customer":
            # 阶段2 才启用 customer JWT 登录；本期仅记录，不覆盖 Cookie client_id
            request.state.auth = actor
