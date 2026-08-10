"""Admin UI 的 hci-sim 控制面代理。

浏览器不能直接访问 Runtime；Gateway 在服务间请求中注入控制面 Token，
并只暴露 build/TestRun 两个受限动作。
"""

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from app.config import settings

router = APIRouter(prefix="/api/hci-sim", tags=["hci-sim"])


async def _post(path: str, payload: dict) -> JSONResponse:
    headers = {"Content-Type": "application/json"}
    if settings.HCI_SIM_CONTROL_TOKEN:
        headers["Authorization"] = f"Bearer {settings.HCI_SIM_CONTROL_TOKEN}"
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(f"{settings.HCI_SIM_URL.rstrip('/')}{path}", json=payload, headers=headers)
    except httpx.RequestError as exc:
        raise HTTPException(status_code=503, detail="hci-sim Runtime unavailable") from exc
    try:
        body = response.json()
    except ValueError:
        body = {"detail": response.text}
    return JSONResponse(content=body, status_code=response.status_code)


@router.post("/v1/simulations/build")
async def build(request: Request) -> JSONResponse:
    return await _post("/v1/simulations/build", await request.json())


@router.post("/v1/simulations/test-runs")
async def create_test_run(request: Request) -> JSONResponse:
    return await _post("/v1/simulations/test-runs", await request.json())
