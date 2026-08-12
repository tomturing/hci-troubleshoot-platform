"""
KB Routes - API Gateway Proxy
将 /api/v1/kb/* 代理到 kb-service:8004/api/kb/*
将 /api/v1/kbd/* 代理到 kb-service:8004/api/admin/kbd/*
"""

import json
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from shared.observability.logger import get_logger

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
    """使用网关自身的 INTERNAL_API_TOKEN 构造鉴权头（下游服务调用）

    防御性校验：token 为空时直接报 500，避免下游 401 难以定位。
    """
    token = settings.INTERNAL_API_TOKEN
    if not token or not str(token).strip():
        raise HTTPException(
            status_code=500,
            detail="INTERNAL_API_TOKEN 未配置，无法调用内部服务",
        )
    return {"Authorization": f"Bearer {str(token).strip()}"}


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
    response = await proxy_request("GET", "/categories", params=dict(request.query_params), headers=headers)
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
    response = await proxy_request("PUT", f"/categories/{code}", payload=body, headers=headers)
    return JSONResponse(content=response.json(), status_code=response.status_code)


@categories_router.post("")
async def create_category_proxy(request: Request):
    """代理分类创建请求 → kb-service"""
    body = await request.json()
    headers = _internal_auth_headers()
    response = await proxy_request("POST", "/categories", payload=body, headers=headers)
    return JSONResponse(content=response.json(), status_code=response.status_code)


@categories_router.delete("/{code}")
async def delete_category_proxy(code: str, request: Request):
    """代理分类删除请求 → kb-service"""
    headers = _internal_auth_headers()
    response = await proxy_request("DELETE", f"/categories/{code}", headers=headers)
    return JSONResponse(content=response.json(), status_code=response.status_code)


@categories_router.put("/{code}/parent")
async def update_category_parent_proxy(code: str, request: Request):
    """代理级联更新父分类层次请求 → kb-service"""
    body = await request.json()
    headers = _internal_auth_headers()
    response = await proxy_request("PUT", f"/categories/{code}/parent", payload=body, headers=headers)
    return JSONResponse(content=response.json(), status_code=response.status_code)


@categories_router.post("/{code}/hit")
async def category_hit_proxy(code: str, request: Request):
    """代理命中计数请求 → kb-service"""
    body = await request.json()
    headers = _internal_auth_headers()
    response = await proxy_request("POST", f"/categories/{code}/hit", payload=body, headers=headers)
    return JSONResponse(content=response.json(), status_code=response.status_code)


@categories_router.post("/import")
async def category_import_proxy(request: Request):
    """代理 YAML 导入请求 → kb-service（透明透传）

    透明透传设计：
    - 网关层直接透传请求体（body）和 Content-Type 头
    - 下游 kb-service 负责解析 multipart/form-data
    - 无需在网关层解析请求，符合"透明代理"原则
    """
    # 直接透传请求体和 Content-Type
    raw_body = await request.body()
    content_type = request.headers.get("content-type", "application/x-yaml")

    headers = _internal_auth_headers()
    headers["content-type"] = content_type

    async with httpx.AsyncClient(timeout=60.0) as client:
        try:
            url = f"{KB_SERVICE_URL}/categories/import"
            resp = await client.post(
                url,
                content=raw_body,
                headers=headers,
                params=dict(request.query_params),
            )
            return JSONResponse(content=resp.json(), status_code=resp.status_code)
        except httpx.RequestError as exc:
            logger.error(f"KB Service 分类导入请求失败: {exc.request.url!r}")
            raise HTTPException(status_code=503, detail="KB Service unavailable") from exc


@categories_router.get("/export")
async def category_export_proxy(request: Request):
    """代理 YAML 导出请求 → kb-service（返回文件流）

    kb-service 返回 StreamingResponse YAML 文件，
    api-gateway 透传文件流和 Content-Disposition 头。
    """
    headers = _internal_auth_headers()

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            url = f"{KB_SERVICE_URL}/categories/export"
            resp = await client.get(url, headers=headers)
            # 透传响应头
            content_disposition = resp.headers.get("content-disposition", "")
            return Response(
                content=resp.content,
                media_type="application/yaml",
                headers={"Content-Disposition": content_disposition} if content_disposition else {},
            )
        except httpx.RequestError as exc:
            logger.error(f"KB Service 分类导出请求失败: {exc.request.url!r}")
            raise HTTPException(status_code=503, detail="KB Service unavailable") from exc


# ============ Resolution Catalogs 代理（admin 前端使用 /api/kb/catalogs 前缀） ============
catalogs_router = APIRouter(prefix="/api/kb/catalogs", tags=["kb-catalogs"])


@catalogs_router.get("")
async def list_catalogs_proxy(request: Request):
    """代理获取 Catalog 列表请求 → kb-service"""
    headers = _internal_auth_headers()
    resp = await proxy_request("GET", "/resolution-catalogs", headers=headers)
    return JSONResponse(content=resp.json(), status_code=resp.status_code)


@catalogs_router.get("/{filename}")
async def get_catalog_proxy(filename: str, request: Request):
    """代理获取指定 Catalog 内容请求 → kb-service"""
    headers = _internal_auth_headers()
    resp = await proxy_request("GET", f"/resolution-catalogs/{filename}", headers=headers)
    return JSONResponse(content=resp.json(), status_code=resp.status_code)


@catalogs_router.post("/{filename}/validate")
async def validate_catalog_proxy(filename: str, request: Request):
    """代理校验 Catalog 内容请求 → kb-service"""
    headers = _internal_auth_headers()
    body = await request.json()
    resp = await proxy_request("POST", f"/resolution-catalogs/{filename}/validate", payload=body, headers=headers)
    return JSONResponse(content=resp.json(), status_code=resp.status_code)


@catalogs_router.put("/{filename}")
async def update_catalog_proxy(filename: str, request: Request):
    """代理更新 Catalog 内容请求 → kb-service"""
    headers = _internal_auth_headers()
    body = await request.json()
    resp = await proxy_request("PUT", f"/resolution-catalogs/{filename}", payload=body, headers=headers)
    return JSONResponse(content=resp.json(), status_code=resp.status_code)


# ============ KBD 审核代理（前端使用 /api/v1/kbd 前缀） ============

KBD_SERVICE_URL = f"{settings.KB_SERVICE_URL}/api/admin/kbd"
kbd_router = APIRouter(prefix="/api/v1/kbd", tags=["kbd"])


async def _kbd_proxy(
    method: str,
    path: str,
    payload: dict | None = None,
    params: dict | None = None,
    headers: dict | None = None,
    timeout: float = 30.0,
):
    """通用代理请求，透传至 kb-service KBD 路由"""
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            url = f"{KBD_SERVICE_URL}{path}"
            response = await client.request(method, url, json=payload, params=params, headers=headers)
            if response.status_code >= 400:
                try:
                    body: Any = response.json()
                except (ValueError, json.JSONDecodeError):
                    body = {"raw_body": response.text[:500]}
                detail = body.get("detail") if isinstance(body, dict) else body
                error = body.get("error") if isinstance(body, dict) else None
                logger.warning(
                    event="kb_downstream_http_error",
                    message="KBD 下游请求返回错误",
                    method=method,
                    path=path,
                    status_code=response.status_code,
                    downstream_code=(error or {}).get("code") if isinstance(error, dict) else (detail or {}).get("code") if isinstance(detail, dict) else None,
                    detail=detail,
                    kbd_id=path.strip("/").split("/")[0] if path.strip("/") else None,
                    signal_id=(detail or {}).get("signal_id") if isinstance(detail, dict) else None,
                    field_path=(detail or {}).get("field_path") if isinstance(detail, dict) else None,
                    downstream_trace_id=response.headers.get("x-trace-id"),
                    downstream_request_id=response.headers.get("x-request-id"),
                )
            return response
        except httpx.RequestError as exc:
            logger.error(f"KB Service KBD 请求失败: {exc.request.url!r}")
            raise HTTPException(status_code=503, detail="KB Service unavailable") from exc


def _kbd_json_response(response: httpx.Response) -> JSONResponse:
    """安全解析下游响应并透传诊断/重试头，避免网关吞掉错误上下文。"""
    try:
        content = response.json()
    except (ValueError, json.JSONDecodeError):
        content = {"detail": response.text[:2000], "code": "DOWNSTREAM_NON_JSON"}
    headers = {
        key: response.headers[key]
        for key in ("x-trace-id", "x-request-id", "retry-after", "content-type")
        if key in response.headers
    }
    return JSONResponse(content=content, status_code=response.status_code, headers=headers)


@kbd_router.get("/pending")
async def kbd_list_proxy(request: Request):
    """代理 KBD 列表请求 → kb-service"""
    headers = _internal_auth_headers()
    response = await _kbd_proxy("GET", "/pending", params=dict(request.query_params), headers=headers)
    return JSONResponse(content=response.json(), status_code=response.status_code)


@kbd_router.get("/pending/stats")
async def kbd_stats_proxy(request: Request):
    """代理 KBD 分类统计请求 → kb-service"""
    headers = _internal_auth_headers()
    response = await _kbd_proxy("GET", "/pending/stats", params=dict(request.query_params), headers=headers)
    return JSONResponse(content=response.json(), status_code=response.status_code)


@kbd_router.post("/tools/convert-safe-pipeline")
async def kbd_convert_safe_pipeline_proxy(request: Request):
    """代理 QFK 安全管道转换预览请求；下游只解析，不执行命令。"""
    body = await request.json()
    headers = _internal_auth_headers()
    response = await _kbd_proxy(
        "POST",
        "/tools/convert-safe-pipeline",
        payload=body,
        headers=headers,
    )
    return JSONResponse(content=response.json(), status_code=response.status_code)


@kbd_router.get("/capabilities")
async def kbd_capabilities_proxy(request: Request):
    """合并共享参数契约与 Agent 当前 Pod 的真实运行时发现结果。"""
    headers = _internal_auth_headers()
    response = await _kbd_proxy("GET", "/capabilities", headers=headers)
    if response.status_code >= 400:
        return JSONResponse(content=response.json(), status_code=response.status_code)
    document = response.json()
    runtime_document = None
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            runtime_response = await client.get(
                f"{settings.AGENT_SERVICE_URL}/internal/capabilities",
                headers=headers,
            )
            if runtime_response.status_code == 200:
                runtime_document = runtime_response.json()
    except httpx.RequestError as exc:
        logger.warning(event="agent_capability_discovery_unavailable", error=str(exc))

    runtime_by_id = {
        item["capability_id"]: item
        for item in (runtime_document or {}).get("capabilities", [])
        if isinstance(item, dict) and item.get("capability_id")
    }
    for descriptor in document.get("capabilities", []):
        runtime = runtime_by_id.get(descriptor.get("capability_id"))
        if runtime:
            descriptor["runtime_status"] = runtime["runtime_status"]
            descriptor["verification_status"] = "runtime_discovered"
            descriptor["runtime"] = runtime
    document["runtime_discovery"] = {
        "status": "available" if runtime_document else "unavailable",
        "service": (runtime_document or {}).get("service"),
    }
    return JSONResponse(content=document, status_code=200)


@kbd_router.post("/command-preview")
async def kbd_command_preview_proxy(request: Request):
    """使用 Agent 当前部署的 QFK Handler 编译只读命令模板。"""

    body = await request.json()
    headers = _internal_auth_headers()
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.post(
                f"{settings.AGENT_SERVICE_URL}/internal/qfk-command-preview",
                json=body,
                headers=headers,
            )
    except httpx.RequestError as exc:
        logger.error(event="qfk_command_preview_unavailable", error=str(exc))
        raise HTTPException(status_code=503, detail="Agent command preview unavailable") from exc
    return JSONResponse(content=response.json(), status_code=response.status_code)


@kbd_router.get("/{kbd_id}/revisions")
async def kbd_revisions_proxy(kbd_id: int, request: Request):
    """代理 KBD Proposal/Expert 历史与当前 runtime active 元数据。"""
    headers = _internal_auth_headers()
    response = await _kbd_proxy("GET", f"/{kbd_id}/revisions", headers=headers)
    return _kbd_json_response(response)


@kbd_router.post("/{kbd_id}/review-signals")
async def kbd_review_signals_proxy(kbd_id: int, request: Request):
    """代理无副作用的 KBD working candidate Signal Review。"""
    headers = _internal_auth_headers()
    response = await _kbd_proxy("POST", f"/{kbd_id}/review-signals", headers=headers)
    return _kbd_json_response(response)


@kbd_router.get("/{kbd_id}")
async def kbd_get_proxy(kbd_id: int, request: Request):
    """代理获取单条 KBD 详情（含 content_md）→ kb-service"""
    headers = _internal_auth_headers()
    response = await _kbd_proxy("GET", f"/{kbd_id}", headers=headers)
    return _kbd_json_response(response)


@kbd_router.patch("/{kbd_id}/approve")
async def kbd_approve_proxy(kbd_id: int, request: Request):
    """代理 KBD 审核通过请求 → kb-service"""
    body = await request.json()
    headers = _internal_auth_headers()
    response = await _kbd_proxy("POST", f"/{kbd_id}/approve", payload=body, headers=headers)
    return _kbd_json_response(response)


@kbd_router.patch("/{kbd_id}/reject")
async def kbd_reject_proxy(kbd_id: int, request: Request):
    """代理 KBD 拒绝请求 → kb-service"""
    body = await request.json()
    headers = _internal_auth_headers()
    response = await _kbd_proxy("PATCH", f"/{kbd_id}/reject", payload=body, headers=headers)
    return _kbd_json_response(response)


@kbd_router.patch("/{kbd_id}")
async def kbd_update_proxy(kbd_id: int, request: Request):
    """代理 KBD 条目内容编辑请求（标题/正文/分类）→ kb-service"""
    body = await request.json()
    headers = _internal_auth_headers()
    response = await _kbd_proxy("PATCH", f"/{kbd_id}", payload=body, headers=headers)
    return _kbd_json_response(response)


@kbd_router.post("/{kbd_id}/maintenance")
async def kbd_create_maintenance_proxy(kbd_id: int, request: Request):
    """创建已发布 KBD 的独立维护工作稿。"""
    headers = _internal_auth_headers()
    response = await _kbd_proxy("POST", f"/{kbd_id}/maintenance", headers=headers)
    return _kbd_json_response(response)


@kbd_router.patch("/{kbd_id}/maintenance")
async def kbd_update_maintenance_proxy(kbd_id: int, request: Request):
    """保存维护工作稿，不切换 Agent active。"""
    body = await request.json()
    headers = _internal_auth_headers()
    response = await _kbd_proxy("PATCH", f"/{kbd_id}/maintenance", payload=body, headers=headers)
    return _kbd_json_response(response)


@kbd_router.delete("/{kbd_id}/maintenance")
async def kbd_discard_maintenance_proxy(kbd_id: int, request: Request):
    """放弃维护工作稿，Agent active 保持不变。"""
    headers = _internal_auth_headers()
    response = await _kbd_proxy("DELETE", f"/{kbd_id}/maintenance", headers=headers)
    return _kbd_json_response(response)


@kbd_router.post("/{kbd_id}/maintenance/publish")
async def kbd_publish_maintenance_proxy(kbd_id: int, request: Request):
    """发布维护工作稿并原子切换 Agent active。"""
    body = await request.json()
    headers = _internal_auth_headers()
    response = await _kbd_proxy(
        "POST",
        f"/{kbd_id}/maintenance/publish",
        payload=body,
        headers=headers,
        timeout=120.0,
    )
    return _kbd_json_response(response)


@kbd_router.post("/{kbd_id}/republish")
async def kbd_republish_proxy(kbd_id: int, request: Request):
    """代理 KBD 重新发布请求（rejected -> published）-> kb-service"""
    body = await request.json()
    headers = _internal_auth_headers()
    response = await _kbd_proxy("POST", f"/{kbd_id}/republish", payload=body, headers=headers)
    return _kbd_json_response(response)


@kbd_router.post("/{kbd_id}/revert-to-draft")
async def kbd_revert_to_draft_proxy(kbd_id: int, request: Request):
    """代理 KBD 退回草稿请求 → kb-service"""
    headers = _internal_auth_headers()
    response = await _kbd_proxy("POST", f"/{kbd_id}/revert-to-draft", headers=headers)
    return _kbd_json_response(response)


@kbd_router.post("/{kbd_id}/reclassify")
async def kbd_reclassify_proxy(kbd_id: int, request: Request):
    """代理 KBD 重新分类请求（用最新 Prompt 重算）-> kb-service"""
    headers = _internal_auth_headers()
    response = await _kbd_proxy("POST", f"/{kbd_id}/reclassify", headers=headers, timeout=120.0)
    return _kbd_json_response(response)


@kbd_router.post("/{kbd_id}/reanalyze-images")
async def kbd_reanalyze_images_proxy(kbd_id: int, request: Request):
    """代理 KBD 重新识图请求（异步提交模式，P1-1）-> kb-service

    新行为：kb-service 立即返回 202 + job_id（秒级），gateway 超时设为 30s。
    客户端通过 GET reanalyze-images/status 轮询完成状态。
    """
    headers = _internal_auth_headers()
    # 透传 query（如 ?sync=true 同步模式开关），否则 kb-service 收不到 sync 而走异步 202
    response = await _kbd_proxy("POST", f"/{kbd_id}/reanalyze-images", params=dict(request.query_params), headers=headers, timeout=30.0)
    return _kbd_json_response(response)


@kbd_router.get("/{kbd_id}/reanalyze-images/status")
async def kbd_reanalyze_status_proxy(kbd_id: int, request: Request):
    """代理 KBD 异步识图状态查询 -> kb-service"""
    headers = _internal_auth_headers()
    response = await _kbd_proxy(
        "GET", f"/{kbd_id}/reanalyze-images/status",
        params=dict(request.query_params),
        headers=headers,
        timeout=15.0,
    )
    return _kbd_json_response(response)


@kbd_router.post("/{kbd_id}/reanalyze-image/{seq}")
async def kbd_reanalyze_single_image_proxy(kbd_id: int, seq: int, request: Request):
    """代理 KBD 单张图片重新识图请求 -> kb-service

    场景：用户在 admin-ui 图片列表中点击单张图片的刷新按钮，
    仅重新识图该图片，不影响其他图片。

    Args:
        kbd_id: KBD 条目 ID
        seq: 图片序号（从 0 开始）
    """
    headers = _internal_auth_headers()
    # 透传 query（如 ?sync=true 同步模式开关），否则 kb-service 收不到 sync 而走异步 202
    response = await _kbd_proxy("POST", f"/{kbd_id}/reanalyze-image/{seq}", params=dict(request.query_params), headers=headers, timeout=240.0)
    return _kbd_json_response(response)


@kbd_router.post("/{kbd_id}/extract-signals")
async def kbd_extract_signals_proxy(kbd_id: int, request: Request):
    """代理 KBD 关键信号分级抽取请求 -> kb-service

    场景：用户在 admin-ui KBD 详情页点击「重新抽取」按钮，用最新 KEY 阶段 Prompt
    重新抽取该条目的结构化关键信号（signals_json）。

    ?sync=true 时 kb-service 同步返回完整结果（signals_count / rejected_count），
    需透传 query，否则 kb-service 走异步 202 + job_id 模式（前端未实现轮询）。
    """
    headers = _internal_auth_headers()
    # 透传 query（?sync=true 同步模式开关），否则 kb-service 收不到 sync 而走异步 202
    response = await _kbd_proxy("POST", f"/{kbd_id}/extract-signals", params=dict(request.query_params), headers=headers, timeout=240.0)
    return _kbd_json_response(response)


# ============ SOP 管理代理（前端使用 /api/v1/sop 前缀） ============

SOP_ADMIN_SERVICE_URL = f"{settings.KB_SERVICE_URL}/api/admin/sop"
SOP_TREE_SERVICE_URL = f"{settings.KB_SERVICE_URL}/api/sop"  # 决策树端点（非 admin 路由）
sop_admin_router = APIRouter(prefix="/api/v1/sop", tags=["sop-admin"])


async def _sop_proxy(
    method: str,
    path: str,
    payload: dict | None = None,
    params: dict | None = None,
    headers: dict | None = None,
    timeout: float = 60.0,
):
    """通用代理请求，透传至 kb-service SOP 管理路由"""
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            url = f"{SOP_ADMIN_SERVICE_URL}{path}"
            response = await client.request(method, url, json=payload, params=params, headers=headers)
            return response
        except httpx.TimeoutException as exc:
            logger.error(
                event="kb_service_sop_request_timeout",
                message="KB Service SOP 请求超时",
                method=method,
                path=path,
                timeout=timeout,
                error=exc,
            )
            raise HTTPException(status_code=504, detail="KB Service 请求超时，请稍后重试") from exc
        except httpx.RequestError as exc:
            logger.error(
                event="kb_service_sop_request_failed",
                message="KB Service SOP 请求失败",
                method=method,
                path=path,
                url=str(exc.request.url),
                error=exc,
            )
            raise HTTPException(status_code=503, detail="KB Service unavailable") from exc


@sop_admin_router.get("")
async def sop_list_proxy(request: Request):
    """代理 SOP 文档列表请求 → kb-service"""
    headers = _internal_auth_headers()
    response = await _sop_proxy("GET", "", params=dict(request.query_params), headers=headers)
    return JSONResponse(content=response.json(), status_code=response.status_code)


@sop_admin_router.get("/{document_id}")
async def sop_detail_proxy(document_id: int, request: Request):
    """代理 SOP 文档详情请求（含 content_md）→ kb-service"""
    headers = _internal_auth_headers()
    response = await _sop_proxy("GET", f"/{document_id}", headers=headers)
    return JSONResponse(content=response.json(), status_code=response.status_code)


@sop_admin_router.get("/{document_id}/tree")
async def sop_tree_proxy(document_id: int, request: Request):
    """代理 SOP 决策树查询请求（返回 tree_json 与元数据）→ kb-service"""
    headers = _internal_auth_headers()
    response = await _sop_proxy("GET", f"/{document_id}/tree", headers=headers)
    return JSONResponse(content=response.json(), status_code=response.status_code)


@sop_admin_router.post("/{document_id}/approve")
async def sop_approve_proxy(document_id: int, request: Request):
    """代理 SOP 文档发布（生成 embedding）→ kb-service，使用 600s 超时应对大文档"""
    body = await request.json()
    headers = _internal_auth_headers()
    # SOP 发布需遍历所有 chunks 生成 embedding，耗时较长，使用独立 600s 超时
    response = await _sop_proxy("POST", f"/{document_id}/approve", payload=body, headers=headers, timeout=600.0)
    try:
        resp_body = response.json()
    except (ValueError, json.JSONDecodeError):
        resp_body = {
            "detail": response.text or "kb-service 返回了非 JSON 响应",
            "status_code": response.status_code,
        }
    return JSONResponse(content=resp_body, status_code=response.status_code)


@sop_admin_router.patch("/{document_id}")
async def sop_update_proxy(document_id: int, request: Request):
    """代理 SOP 文档状态更新（归档等）→ kb-service"""
    body = await request.json()
    headers = _internal_auth_headers()
    response = await _sop_proxy("PATCH", f"/{document_id}", payload=body, headers=headers)
    return JSONResponse(content=response.json(), status_code=response.status_code)


@sop_admin_router.post("/upload")
async def sop_upload_proxy(request: Request):
    """透传 .docx 文件上传请求（multipart/form-data）→ kb-service"""
    raw_body = await request.body()
    content_type = request.headers.get("content-type", "")
    headers = _internal_auth_headers()
    headers["content-type"] = content_type

    async with httpx.AsyncClient(timeout=120.0) as client:
        try:
            url = f"{SOP_ADMIN_SERVICE_URL}/upload"
            resp = await client.post(url, content=raw_body, headers=headers)
            return JSONResponse(content=resp.json(), status_code=resp.status_code)
        except httpx.RequestError as exc:
            logger.error(f"KB Service SOP 上传请求失败: {exc.request.url!r}")
            raise HTTPException(status_code=503, detail="KB Service unavailable") from exc
