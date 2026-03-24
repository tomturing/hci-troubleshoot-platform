"""
Atoms Routes — API Gateway 代理
将 /api/v1/atoms/* 代理到 kb-service:8004/api/v1/atoms/*

供管理后台调用知识原子 CRUD 接口（T17 知识反馈闭环）
"""

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from shared.utils.logger import get_logger

from app.config import settings

router = APIRouter(prefix="/api/v1/atoms", tags=["atoms"])
logger = get_logger("gateway-atoms")

# kb-service 知识原子接口直接路径（独立于 /api/kb 前缀）
_KB_ATOMS_BASE = f"{settings.KB_SERVICE_URL}/api/v1/atoms"


async def _proxy(method: str, path: str, request: Request, payload: dict | None = None) -> JSONResponse:
    """通用代理请求，透传 Authorization 头"""
    url = f"{_KB_ATOMS_BASE}{path}"
    headers: dict = {}
    if auth := request.headers.get("Authorization"):
        headers["Authorization"] = auth

    async with httpx.AsyncClient(timeout=20.0) as client:
        try:
            resp = await client.request(
                method,
                url,
                json=payload,
                params=dict(request.query_params),
                headers=headers,
            )
            # 204 No Content 直接返回，无 body
            if resp.status_code == 204:
                return Response(status_code=204)  # type: ignore[return-value]
            return JSONResponse(content=resp.json(), status_code=resp.status_code)
        except httpx.RequestError as exc:
            logger.error(
                event="atoms_proxy_error",
                message=str(exc),
                url=str(url),
            )
            raise HTTPException(status_code=503, detail="KB Service unavailable") from exc


@router.get("/pending")
async def list_pending(request: Request):
    """列出待审核知识原子（verified=false）"""
    return await _proxy("GET", "/pending", request)


@router.get("/{atom_id}")
async def get_atom(atom_id: str, request: Request):
    """获取知识原子详情"""
    return await _proxy("GET", f"/{atom_id}", request)


@router.post("")
async def create_atom(request: Request):
    """写入新知识原子（由 KnowledgeExtractor 调用，需要内部 Token）"""
    body = await request.json()
    return await _proxy("POST", "", request, payload=body)


@router.patch("/{atom_id}/verify")
async def verify_atom(atom_id: str, request: Request):
    """审核通过知识原子"""
    body = await request.json()
    return await _proxy("PATCH", f"/{atom_id}/verify", request, payload=body)


@router.patch("/{atom_id}")
async def update_atom(atom_id: str, request: Request):
    """编辑修正知识原子内容"""
    body = await request.json()
    return await _proxy("PATCH", f"/{atom_id}", request, payload=body)


@router.delete("/{atom_id}")
async def delete_atom(atom_id: str, request: Request):
    """拒绝并删除知识原子"""
    return await _proxy("DELETE", f"/{atom_id}", request)
