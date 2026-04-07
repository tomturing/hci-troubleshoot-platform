"""
KB Routes - API Gateway Proxy
将 /api/v1/kb/* 代理到 kb-service:8004/api/kb/*
将 /api/v1/kbd/* 代理到 kb-service:8004/api/admin/kbd/*
"""

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from shared.utils.logger import get_logger

from app.config import settings

router = APIRouter(prefix="/api/v1/kb", tags=["kb"])
logger = get_logger("gateway-kb")

KB_SERVICE_URL = f"{settings.KB_SERVICE_URL}/api/kb"


async def proxy_request(
    method: str,
    path: str,
    payload: dict | None = None,
    params: dict | None = None,
    headers: dict | None = None,
):
    """通用代理请求，透传至 kb-service"""
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            url = f"{KB_SERVICE_URL}{path}"
            response = await client.request(method, url, json=payload, params=params, headers=headers)
            return response
        except httpx.RequestError as exc:
            logger.error(f"KB Service 请求失败: {exc.request.url!r}")
            raise HTTPException(status_code=503, detail="KB Service unavailable") from exc


def _forward_headers(request: Request) -> dict:
    """透传 Authorization 头（内部 Token）"""
    headers = {}
    if auth := request.headers.get("Authorization"):
        headers["Authorization"] = auth
    return headers


def _internal_auth_headers() -> dict:
    """使用网关自身的 INTERNAL_API_TOKEN 构造鉴权头（下游服务调用）"""
    return {"Authorization": f"Bearer {settings.INTERNAL_API_TOKEN}"}


# ============ 搜索接口（公开）============


@router.post("/search")
async def search(request: Request):
    """混合检索（BM25 + 向量 RRF 融合）"""
    body = await request.json()
    response = await proxy_request("POST", "/search", payload=body)
    return JSONResponse(content=response.json(), status_code=response.status_code)


@router.post("/sop/match")
async def sop_match(request: Request):
    """SOP 关键词精确匹配"""
    body = await request.json()
    response = await proxy_request("POST", "/sop/match", payload=body)
    return JSONResponse(content=response.json(), status_code=response.status_code)


# ============ 写入接口（需 Internal Token）============


@router.post("/ingest")
async def ingest(request: Request):
    """文档摄入（SHA256 幂等）"""
    body = await request.json()
    headers = _forward_headers(request)
    response = await proxy_request("POST", "/ingest", payload=body, headers=headers)
    return JSONResponse(content=response.json(), status_code=response.status_code)


@router.post("/sop/import")
async def sop_import(request: Request):
    """SOP 节点批量导入"""
    body = await request.json()
    headers = _forward_headers(request)
    response = await proxy_request("POST", "/sop/import", payload=body, headers=headers)
    return JSONResponse(content=response.json(), status_code=response.status_code)


# ============ 管理接口（需 Internal Token）============


@router.get("/documents")
async def list_documents(request: Request):
    """文档列表"""
    headers = _forward_headers(request)
    response = await proxy_request("GET", "/documents", params=dict(request.query_params), headers=headers)
    return JSONResponse(content=response.json(), status_code=response.status_code)


@router.get("/documents/{doc_id}")
async def get_document(doc_id: int, request: Request):
    """获取文档详情"""
    headers = _forward_headers(request)
    response = await proxy_request("GET", f"/documents/{doc_id}", headers=headers)
    return JSONResponse(content=response.json(), status_code=response.status_code)


@router.patch("/documents/{doc_id}")
async def update_document_status(doc_id: int, request: Request):
    """更新文档状态"""
    body = await request.json()
    headers = _forward_headers(request)
    response = await proxy_request("PATCH", f"/documents/{doc_id}", payload=body, headers=headers)
    return JSONResponse(content=response.json(), status_code=response.status_code)


@router.delete("/documents/{doc_id}")
async def delete_document(doc_id: int, request: Request):
    """删除文档"""
    headers = _forward_headers(request)
    response = await proxy_request("DELETE", f"/documents/{doc_id}", headers=headers)
    return JSONResponse(content=response.json(), status_code=response.status_code)


# ============ 分类管理代理（admin 前端使用 /api/kb/categories 前缀） ============

categories_router = APIRouter(prefix="/api/kb/categories", tags=["kb-categories"])


@categories_router.get("")
async def list_categories_proxy(request: Request):
    """代理分类列表请求 → kb-service"""
    headers = _internal_auth_headers()
    response = await proxy_request(
        "GET", "/categories", params=dict(request.query_params), headers=headers
    )
    return JSONResponse(content=response.json(), status_code=response.status_code)


@categories_router.get("/stats")
async def category_stats_proxy(request: Request):
    """代理分类统计请求 → kb-service"""
    headers = _internal_auth_headers()
    response = await proxy_request("GET", "/categories/stats", headers=headers)
    return JSONResponse(content=response.json(), status_code=response.status_code)


@categories_router.put("/{code}")
async def update_category_proxy(code: str, request: Request):
    """代理分类更新请求 → kb-service"""
    body = await request.json()
    headers = _internal_auth_headers()
    response = await proxy_request(
        "PUT", f"/categories/{code}", payload=body, headers=headers
    )
    return JSONResponse(content=response.json(), status_code=response.status_code)


@categories_router.post("/{code}/hit")
async def category_hit_proxy(code: str, request: Request):
    """代理命中计数请求 → kb-service"""
    body = await request.json()
    headers = _internal_auth_headers()
    response = await proxy_request(
        "POST", f"/categories/{code}/hit", payload=body, headers=headers
    )
    return JSONResponse(content=response.json(), status_code=response.status_code)


@categories_router.post("/import")
async def category_import_proxy(request: Request):
    """代理 YAML 导入请求 → kb-service。

    下游 kb-service 读取的是 YAML 原文字节流，不支持将 multipart/form-data 原样透传。
    因此这里需要在网关层对 multipart 上传进行解包，只转发文件内容本身。
    """
    headers = _internal_auth_headers()
    content_type = request.headers.get("content-type", "")
    outbound_headers = dict(headers)

    if content_type.startswith("multipart/form-data"):
        form = await request.form()
        upload = (
            form.get("file")
            or form.get("yaml_file")
            or form.get("upload")
            or form.get("content")
        )
        if upload is None or not hasattr(upload, "read"):
            raise HTTPException(
                status_code=400, detail="缺少 YAML 导入文件，请使用 multipart 文件字段 file 上传"
            )

        raw_body = await upload.read()
        file_content_type = getattr(upload, "content_type", None) or ""
        outbound_headers["content-type"] = (
            file_content_type
            if file_content_type in {"application/x-yaml", "text/yaml", "application/yaml", "text/plain"}
            else "application/x-yaml"
        )
    else:
        raw_body = await request.body()
        outbound_headers["content-type"] = content_type or "application/x-yaml"

    async with httpx.AsyncClient(timeout=60.0) as client:
        try:
            url = f"{KB_SERVICE_URL}/categories/import"
            resp = await client.post(
                url,
                content=raw_body,
                headers=outbound_headers,
                params=dict(request.query_params),
            )
            return JSONResponse(content=resp.json(), status_code=resp.status_code)
        except httpx.RequestError as exc:
            logger.error(f"KB Service 分类导入请求失败: {exc.request.url!r}")
            raise HTTPException(
                status_code=503, detail="KB Service unavailable"
            ) from exc


# ============ KBD 审核代理（前端使用 /api/v1/kbd 前缀） ============

KBD_SERVICE_URL = f"{settings.KB_SERVICE_URL}/api/admin/kbd"
kbd_router = APIRouter(prefix="/api/v1/kbd", tags=["kbd"])


async def _kbd_proxy(
    method: str,
    path: str,
    payload: dict | None = None,
    params: dict | None = None,
    headers: dict | None = None,
):
    """通用代理请求，透传至 kb-service KBD 路由"""
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            url = f"{KBD_SERVICE_URL}{path}"
            response = await client.request(method, url, json=payload, params=params, headers=headers)
            return response
        except httpx.RequestError as exc:
            logger.error(f"KB Service KBD 请求失败: {exc.request.url!r}")
            raise HTTPException(status_code=503, detail="KB Service unavailable") from exc


@kbd_router.get("/pending")
async def kbd_list_proxy(request: Request):
    """代理 KBD 列表请求 → kb-service"""
    headers = _internal_auth_headers()
    response = await _kbd_proxy("GET", "/pending", params=dict(request.query_params), headers=headers)
    return JSONResponse(content=response.json(), status_code=response.status_code)


@kbd_router.patch("/{kbd_id}/approve")
async def kbd_approve_proxy(kbd_id: int, request: Request):
    """代理 KBD 审核通过请求 → kb-service"""
    body = await request.json()
    headers = _internal_auth_headers()
    response = await _kbd_proxy("POST", f"/{kbd_id}/approve", payload=body, headers=headers)
    return JSONResponse(content=response.json(), status_code=response.status_code)


@kbd_router.patch("/{kbd_id}/reject")
async def kbd_reject_proxy(kbd_id: int, request: Request):
    """代理 KBD 拒绝请求 → kb-service"""
    body = await request.json()
    headers = _internal_auth_headers()
    response = await _kbd_proxy("PATCH", f"/{kbd_id}/reject", payload=body, headers=headers)
    return JSONResponse(content=response.json(), status_code=response.status_code)
