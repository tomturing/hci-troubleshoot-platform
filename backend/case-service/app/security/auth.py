"""
安全模块 - 服务间身份验签 + 工单归属校验（BOLA 防护，P0 修复）

信任模型（与 conversation-service 对齐）：
- 只接受网关以共享密钥（INTERNAL_API_TOKEN）HMAC 签名注入的 X-Client-ID。
  用户直连本服务伪造身份一律拒绝。
- 查询 case 表的 client_id 与签名身份比对，不匹配即按 404 处理（避免泄露
  资源存在性）。
- admin 端点要求 Authorization: Bearer INTERNAL_API_TOKEN。
"""

import hmac

from app.config import settings
from fastapi import Header, HTTPException, Request
from shared.security.signature import verify_client_identity
from sqlalchemy import text


async def authenticate_request(request: Request) -> str | None:
    """验证请求身份签名，返回 client_id 或 None。"""
    return verify_client_identity(request.headers, settings.INTERNAL_API_TOKEN)


async def get_client_id(request: Request) -> str:
    """从签名身份头解析 client_id；缺失/无效则 401。"""
    client_id = await authenticate_request(request)
    if not client_id:
        raise HTTPException(status_code=401, detail="身份签名缺失或无效")
    return client_id


async def verify_case_ownership(case_id: str, client_id: str, session) -> None:
    """校验工单归属当前 client_id；不存在或非归属统一 404，避免泄露。"""
    res = await session.execute(text('SELECT client_id FROM "case" WHERE case_id = :cid'), {"cid": case_id})
    row = res.fetchone()
    if row is None or row[0] != client_id:
        raise HTTPException(status_code=404, detail="Case not found")


def require_admin_token(authorization: str | None = Header(default=None)):
    """Admin 端点鉴权：要求 Authorization: Bearer INTERNAL_API_TOKEN（防时序侧信道）。"""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=403, detail="需要管理员凭证")
    token = authorization[7:]
    if not hmac.compare_digest(token, settings.INTERNAL_API_TOKEN):
        raise HTTPException(status_code=403, detail="管理员凭证无效")
