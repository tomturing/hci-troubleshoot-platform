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

CASE_SERVICE_URL = f"{settings.CASE_SERVICE_URL}/api/cases"
SCHEDULER_SERVICE_URL = settings.SCHEDULER_SERVICE_URL

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

# ============ Admin 路由（静态路径，放在 {case_id} 之前）============

@router.get("/all")
async def list_all_cases(request: Request):
    """[Admin] 获取所有工单列表"""
    response = await proxy_request("GET", "/all", params=dict(request.query_params))
    return JSONResponse(content=response.json(), status_code=response.status_code)

@router.get("/stats")
async def get_case_stats(request: Request):
    """[Admin] 获取工单统计"""
    response = await proxy_request("GET", "/stats")
    return JSONResponse(content=response.json(), status_code=response.status_code)

@router.get("/clients")
async def get_client_list(request: Request):
    """[Admin] 获取客户端列表"""
    response = await proxy_request("GET", "/clients")
    return JSONResponse(content=response.json(), status_code=response.status_code)

# ============ 客户端路由 ============

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
    """关闭工单，并释放关联的 Pod 资源"""
    response = await proxy_request("PUT", f"/{case_id}/close")
    if response.status_code == 404:
        raise HTTPException(status_code=404, detail="Case not found")
    
    # 工单关闭成功后，通知 Scheduler 释放关联的 Pod，避免资源泄漏
    if response.status_code == 200:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                release_resp = await client.post(
                    f"{settings.SCHEDULER_SERVICE_URL}/api/scheduler/pods/release",
                    json={"case_id": case_id}
                )
                if release_resp.status_code == 200:
                    logger.info(f"Released pod for closed case {case_id}")
                else:
                    logger.warning(f"Pod release returned {release_resp.status_code} for case {case_id}")
        except Exception as e:
            # Pod 释放失败不影响工单关闭结果
            logger.warning(f"Failed to release pod for case {case_id}: {e}")
    
    return JSONResponse(content=response.json(), status_code=response.status_code)
