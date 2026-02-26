"""
Case Routes - API Gateway Proxy
"""

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse
import httpx
from typing import Optional

from app.config import settings
from shared.utils.logger import get_logger

router = APIRouter(prefix="/api/cases", tags=["cases"])
logger = get_logger("gateway-cases")

CASE_SERVICE_URL = f"http://case-service:{settings.CASE_SERVICE_PORT}/api/cases"

async def proxy_request(
    method: str,
    path: str,
    payload: Optional[dict] = None,
    params: Optional[dict] = None,
    headers: Optional[dict] = None
):
    async with httpx.AsyncClient() as client:
        try:
            url = f"{CASE_SERVICE_URL}{path}"
            response = await client.request(
                method,
                url,
                json=payload,
                params=params,
                headers=headers
            )
            return response
        except httpx.RequestError as exc:
            logger.error(f"Error requesting {exc.request.url!r}.")
            raise HTTPException(status_code=503, detail="Service unavailable")

@router.post("/")
async def create_case(request: Request):
    """创建工单"""
    payload = await request.json()
    response = await proxy_request("POST", "/", payload)
    return JSONResponse(content=response.json(), status_code=response.status_code)

@router.get("/{case_id}")
async def get_case(case_id: str, request: Request):
    """获取工单详情"""
    response = await proxy_request("GET", f"/{case_id}")
    if response.status_code == 404:
        raise HTTPException(status_code=404, detail="Case not found")
    return JSONResponse(content=response.json(), status_code=response.status_code)

@router.get("/")
async def list_cases(client_id: str, request: Request):
    """查询工单列表"""
    response = await proxy_request("GET", "/", params={"client_id": client_id})
    return JSONResponse(content=response.json(), status_code=response.status_code)

@router.put("/{case_id}/confirm")
async def confirm_case(case_id: str, request: Request):
    """确认工单"""
    response = await proxy_request("PUT", f"/{case_id}/confirm")
    if response.status_code == 404:
        raise HTTPException(status_code=404, detail="Case not found")
    return JSONResponse(content=response.json(), status_code=response.status_code)

@router.put("/{case_id}/close")
async def close_case(case_id: str, request: Request):
    """关闭工单"""
    response = await proxy_request("PUT", f"/{case_id}/close")
    if response.status_code == 404:
        raise HTTPException(status_code=404, detail="Case not found")
    return JSONResponse(content=response.json(), status_code=response.status_code)
