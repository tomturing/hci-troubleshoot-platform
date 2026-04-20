"""
Environment Routes - API Gateway Proxy

代理转发到 case-service 的环境数据接口。
"""

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from shared.utils.logger import get_logger

from app.config import settings

router = APIRouter(prefix="/api/environments", tags=["environments"])

CASE_SERVICE_URL = settings.CASE_SERVICE_URL
logger = get_logger("gateway-environments")


async def proxy_request(
    method: str,
    path: str,
    payload: dict | None = None,
    params: dict | None = None,
):
    """代理请求到 case-service"""
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            url = f"{CASE_SERVICE_URL}/api/environments{path}"
            response = await client.request(
                method,
                url,
                json=payload,
                params=params,
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
async def create_environment(request: Request):
    """创建环境数据"""
    payload = await request.json()
    response = await proxy_request("POST", "/", payload)
    return JSONResponse(content=response.json(), status_code=response.status_code)


@router.get("/case/{case_id}")
async def get_environments_by_case(case_id: str):
    """获取工单所有环境数据"""
    response = await proxy_request("GET", f"/case/{case_id}")
    return JSONResponse(content=response.json(), status_code=response.status_code)


@router.get("/case/{case_id}/type/{env_type}")
async def get_environment_by_type(case_id: str, env_type: str):
    """获取工单指定类型环境数据"""
    response = await proxy_request("GET", f"/case/{case_id}/type/{env_type}")
    if response.status_code == 404:
        raise HTTPException(status_code=404, detail="Environment data not found")
    if response.status_code == 400:
        raise HTTPException(status_code=400, detail=response.json().get("detail", "Invalid env_type"))
    return JSONResponse(content=response.json(), status_code=response.status_code)


@router.get("/case/{case_id}/context")
async def get_environment_context(case_id: str):
    """获取 S0 阶段 Prompt 构建所需的环境上下文"""
    response = await proxy_request("GET", f"/case/{case_id}/context")
    return JSONResponse(content=response.json(), status_code=response.status_code)
