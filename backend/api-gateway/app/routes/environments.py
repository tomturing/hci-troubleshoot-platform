"""
Environment Routes - API Gateway Proxy

P0 安全修复：所有端点要求服务端签发的身份 Cookie（require_user），并向下游
注入 HMAC 签名身份头，供 case-service 做工单归属校验（BOLA 防护）。
"""

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from shared.observability.logger import get_logger
from shared.security.signature import sign_client_identity

from app.config import settings
from app.security.gateway_auth import require_user

router = APIRouter(prefix="/api/environments", tags=["environments"])

CASE_SERVICE_URL = settings.CASE_SERVICE_URL
logger = get_logger("gateway-environments")


def _signed_headers(client_id: str) -> dict:
    """为下游注入 HMAC 签名身份头（client_id 来自服务端 Cookie）。"""
    return sign_client_identity(client_id, settings.INTERNAL_API_TOKEN)


async def proxy_request(
    method: str,
    path: str,
    client_id: str,
    payload: dict | None = None,
    params: dict | None = None,
):
    """代理请求到 case-service（携带签名身份头）"""
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            url = f"{CASE_SERVICE_URL}/api/environments{path}"
            response = await client.request(
                method,
                url,
                json=payload,
                params=params,
                headers=_signed_headers(client_id),
            )
            return response
        except httpx.RequestError as exc:
            logger.error(
                event="proxy_request_error",
                message=f"Error requesting {exc.request.url!r}",
                path=path,
            )
            raise HTTPException(status_code=503, detail="Case Service unavailable")


@router.post("/")
async def create_environment(request: Request, client_id: str = Depends(require_user)):
    """创建环境数据"""
    payload = await request.json()
    response = await proxy_request("POST", "/", client_id, payload)
    return JSONResponse(content=response.json(), status_code=response.status_code)


@router.put("/case/{case_id}/type/{env_type}")
async def upsert_environment(case_id: str, env_type: str, request: Request, client_id: str = Depends(require_user)):
    """upsert 环境数据（幂等：有则更新，无则创建）"""
    payload = await request.json()
    response = await proxy_request("PUT", f"/case/{case_id}/type/{env_type}", client_id, payload)
    if response.status_code == 400:
        raise HTTPException(status_code=400, detail=response.json().get("detail", "Invalid env_type"))
    return JSONResponse(content=response.json(), status_code=response.status_code)


@router.get("/case/{case_id}")
async def get_environments_by_case(case_id: str, client_id: str = Depends(require_user)):
    """获取工单所有环境数据（归属校验在 case-service 完成）"""
    response = await proxy_request("GET", f"/case/{case_id}", client_id)
    return JSONResponse(content=response.json(), status_code=response.status_code)


@router.get("/case/{case_id}/type/{env_type}")
async def get_environment_by_type(case_id: str, env_type: str, client_id: str = Depends(require_user)):
    """获取工单指定类型环境数据（归属校验在 case-service 完成）"""
    response = await proxy_request("GET", f"/case/{case_id}/type/{env_type}", client_id)
    if response.status_code == 404:
        raise HTTPException(status_code=404, detail="Environment data not found")
    if response.status_code == 400:
        raise HTTPException(status_code=400, detail=response.json().get("detail", "Invalid env_type"))
    return JSONResponse(content=response.json(), status_code=response.status_code)


@router.get("/case/{case_id}/context")
async def get_environment_context(case_id: str, client_id: str = Depends(require_user)):
    """获取 S0 阶段 Prompt 构建所需的环境上下文（归属校验在 case-service 完成）"""
    response = await proxy_request("GET", f"/case/{case_id}/context", client_id)
    return JSONResponse(content=response.json(), status_code=response.status_code)
