"""认证路由：双 realm 登录端点 + JWKS 端点。

- POST /api/platform-auth/customer/login  → realm=customer，签发 aud=hci-customer
- POST /api/platform-auth/admin/login      → realm=admin，签发 aud=hci-admin（短时效）
- GET  /.well-known/jwks.json     → 公钥（网关/下游验签）

登录结果：返回 Bearer access_token（JSON 体）+ 下发 HttpOnly Session Cookie
（关键操作二次校验防 CSRF）。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from app.config import settings
from app.security.jwt import issue_token
from app.services.repository import AuthError, authenticate_by_password

router = APIRouter()


class LoginRequest(BaseModel):
    identifier: str
    password: str


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "Bearer"
    expires_in: int


def _client_meta(request: Request) -> tuple[str | None, str | None]:
    return (request.client.host if request.client else None), request.headers.get("User-Agent")


@router.post("/api/platform-auth/customer/login", response_model=LoginResponse)
async def customer_login(req: LoginRequest, request: Request, response: Response):
    return await _login(
        "customer",
        settings.JWT_AUD_CUSTOMER,
        settings.ACCESS_TOKEN_TTL_CUSTOMER,
        "hci_cust_session",
        req,
        request,
        response,
    )


@router.post("/api/platform-auth/admin/login", response_model=LoginResponse)
async def admin_login(req: LoginRequest, request: Request, response: Response):
    return await _login(
        "admin",
        settings.JWT_AUD_ADMIN,
        settings.ACCESS_TOKEN_TTL_ADMIN,
        "hci_admin_session",
        req,
        request,
        response,
    )


async def _login(
    realm: str,
    aud: str,
    ttl: int,
    cookie_name: str,
    req: LoginRequest,
    request: Request,
    response: Response,
) -> LoginResponse:
    ip, ua = _client_meta(request)
    trace_id = request.headers.get("X-Trace-Id", str(uuid.uuid4()))
    try:
        result = await authenticate_by_password(
            realm=realm,
            identifier=req.identifier,
            password=req.password,
            ip=ip,
            ua=ua,
            trace_id=trace_id,
        )
    except AuthError as exc:
        detail = {
            "invalid_credentials": "用户名或密码错误",
            "locked": "账号已锁定，请稍后重试",
            "disabled": "账号已被停用",
            "role_violation": "账号角色配置异常，请联系管理员",
        }.get(exc.args[0], "认证失败")
        raise HTTPException(status_code=401, detail=detail)

    token = issue_token(
        user_id=result["user_id"],
        realm=realm,
        roles=result["roles"],
        aud=aud,
        token_version=result["token_version"],
        ttl=ttl,
    )
    response.set_cookie(
        cookie_name,
        token,
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=ttl,
        path="/",
    )
    return LoginResponse(access_token=token, expires_in=ttl)


@router.get("/.well-known/jwks.json")
async def jwks():
    from app.security.keys import get_jwks

    return get_jwks()
