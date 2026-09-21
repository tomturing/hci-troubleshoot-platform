"""
网关鉴权依赖（P0 修复）

- require_user: 强制要求服务端签发的身份 Cookie，返回其中的 client_id。
- require_admin: 强制要求 Authorization: Bearer INTERNAL_API_TOKEN。
"""

from fastapi import HTTPException, Request


async def require_user(request: Request) -> str:
    """强制已认证用户身份，返回 client_id。

    - 管理后台（携带 INTERNAL_API_TOKEN）：优先使用其显式指定的 client_id 查询
      参数，用于以指定客户身份执行操作（信任 admin 令牌，与既有 admin 路由一致）。
    - 普通用户：返回服务端签发 Cookie 中的 client_id（不可伪造）。
    """
    if getattr(request.state, "is_admin", False):
        admin_cid = request.query_params.get("client_id")
        if admin_cid:
            return admin_cid
    cid = getattr(request.state, "client_id", None)
    if cid:
        return cid
    raise HTTPException(status_code=401, detail="未认证：缺少有效身份 Cookie")


async def require_admin(request: Request) -> None:
    """强制管理员凭证（INTERNAL_API_TOKEN）。"""
    if not getattr(request.state, "is_admin", False):
        raise HTTPException(status_code=403, detail="需要管理员凭证")
