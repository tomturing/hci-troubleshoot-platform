"""
Case Routes - API Gateway Proxy

P0 安全修复：
- admin 端点（/all、/stats、/clients、PUT /{case_id}）要求 INTERNAL_API_TOKEN（require_admin），
  并透传 Authorization 头供 case-service 校验。
- 客户端端点要求服务端签发的身份 Cookie（require_user）；client_id 一律以
  Cookie 解析值覆盖，忽略请求参数/头中自报的 client_id，杜绝越权枚举。
- 向下游注入 HMAC 签名身份头，供 case-service 做归属校验（BOLA 防护）。
- 列表接口强制 limit / 单页上限，防止整批拉走。
"""

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from prometheus_client import Counter
from shared.observability.logger import get_logger
from shared.security.signature import sign_client_identity

from app.config import settings
from app.security.gateway_auth import require_admin, require_user

router = APIRouter(prefix="/api/cases", tags=["cases"])
logger = get_logger("gateway-cases")

# H-3：Pod 释放失败指标，非静默（用于 Alert 规则 PodReleaseFailures）
POD_RELEASE_FAILURES_TOTAL = Counter(
    "hci_pod_release_failures_total",
    "Pod 释放失败次数（工单关闭时）",
    ["reason"],
)

CASE_SERVICE_URL = f"{settings.CASE_SERVICE_URL}/api/cases"
SCHEDULER_SERVICE_URL = settings.SCHEDULER_SERVICE_URL

# 单页上限（防整批拉走）
MAX_PAGE_LIMIT = 100


def _admin_headers(request: Request) -> dict:
    """透传管理员 Authorization 头（已是 require_admin 校验过的请求）。"""
    auth = request.headers.get("Authorization")
    return {"Authorization": auth} if auth else {}


def _signed_headers(client_id: str) -> dict:
    """为下游注入 HMAC 签名身份头（client_id 来自服务端 Cookie）。"""
    return sign_client_identity(client_id, settings.INTERNAL_API_TOKEN)


async def proxy_request(
    method: str, path: str, payload: dict | None = None, params: dict | None = None, headers: dict | None = None
):
    async with httpx.AsyncClient() as client:
        try:
            url = f"{CASE_SERVICE_URL}{path}"
            response = await client.request(method, url, json=payload, params=params, headers=headers)
            return response
        except httpx.RequestError as exc:
            logger.error(f"Error requesting {exc.request.url!r}.")
            raise HTTPException(status_code=503, detail="Service unavailable")


# ============ Admin 路由（静态路径，放在 {case_id} 之前）============


@router.get("/all")
async def list_all_cases(request: Request, _: None = Depends(require_admin)):
    """[Admin] 获取所有工单列表（要求管理员凭证）"""
    response = await proxy_request("GET", "/all", params=dict(request.query_params), headers=_admin_headers(request))
    return JSONResponse(content=response.json(), status_code=response.status_code)


@router.get("/stats")
async def get_case_stats(request: Request, _: None = Depends(require_admin)):
    """[Admin] 获取工单统计（要求管理员凭证）"""
    response = await proxy_request("GET", "/stats", headers=_admin_headers(request))
    return JSONResponse(content=response.json(), status_code=response.status_code)


@router.get("/clients")
async def get_client_list(request: Request, _: None = Depends(require_admin)):
    """[Admin] 获取客户端列表（要求管理员凭证）"""
    response = await proxy_request("GET", "/clients", headers=_admin_headers(request))
    return JSONResponse(content=response.json(), status_code=response.status_code)


# ============ 客户端路由 ============


@router.post("/")
async def create_case(request: Request, client_id: str = Depends(require_user)):
    """创建工单（client_id 由服务端身份 Cookie 决定，覆盖自报值）"""
    payload = await request.json()
    payload["client_id"] = client_id
    response = await proxy_request("POST", "/", payload, headers=_signed_headers(client_id))
    return JSONResponse(content=response.json(), status_code=response.status_code)


@router.get("/{case_id}")
async def get_case(case_id: str, client_id: str = Depends(require_user)):
    """获取工单详情（归属校验在 case-service 完成）"""
    response = await proxy_request("GET", f"/{case_id}", headers=_signed_headers(client_id))
    if response.status_code == 404:
        raise HTTPException(status_code=404, detail="Case not found")
    return JSONResponse(content=response.json(), status_code=response.status_code)


@router.get("/")
async def list_cases(
    client_id: str = Depends(require_user),
    limit: int = Query(default=20, ge=1, le=MAX_PAGE_LIMIT, description="每页数量"),
    offset: int = Query(default=0, ge=0, description="偏移量"),
):
    """查询当前身份下的工单列表（client_id 来自 Cookie，强制分页）"""
    params = {"client_id": client_id, "limit": limit, "offset": offset}
    response = await proxy_request("GET", "/", params=params, headers=_signed_headers(client_id))
    return JSONResponse(content=response.json(), status_code=response.status_code)


@router.put("/{case_id}/confirm")
async def confirm_case(case_id: str, client_id: str = Depends(require_user)):
    """确认工单（归属校验在 case-service 完成）"""
    response = await proxy_request("PUT", f"/{case_id}/confirm", headers=_signed_headers(client_id))
    if response.status_code == 404:
        raise HTTPException(status_code=404, detail="Case not found")
    return JSONResponse(content=response.json(), status_code=response.status_code)


@router.put("/{case_id}/close")
async def close_case(case_id: str, client_id: str = Depends(require_user)):
    """关闭工单，并释放关联的 Pod 资源"""
    response = await proxy_request("PUT", f"/{case_id}/close", headers=_signed_headers(client_id))
    if response.status_code == 404:
        raise HTTPException(status_code=404, detail="Case not found")

    # 工单关闭成功后，通知 Scheduler 释放关联的 Pod，避免资源泄漏
    if response.status_code == 200:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                release_resp = await client.post(
                    f"{settings.SCHEDULER_SERVICE_URL}/api/scheduler/pods/release", json={"case_id": case_id}
                )
                if release_resp.status_code == 200:
                    logger.info(f"Released pod for closed case {case_id}")
                else:
                    logger.warning(f"Pod release returned {release_resp.status_code} for case {case_id}")
        except Exception as e:
            # Pod 释放失败不阻断工单关闭，但推送 Prometheus 指标（非静默！H-3）
            logger.warning(
                event="pod_release_failed",
                message=f"Pod 释放失败，case_id={case_id}",
                case_id=case_id,
                error=str(e),
                error_type=type(e).__name__,
            )
            POD_RELEASE_FAILURES_TOTAL.labels(reason=type(e).__name__).inc()

    return JSONResponse(content=response.json(), status_code=response.status_code)


@router.put("/{case_id}")
async def update_case(case_id: str, request: Request, _: None = Depends(require_admin)):
    """[Admin] 编辑工单（要求管理员凭证）"""
    payload = await request.json()
    response = await proxy_request("PUT", f"/{case_id}", payload, headers=_admin_headers(request))
    if response.status_code == 404:
        raise HTTPException(status_code=404, detail="Case not found")

    # 如果工单被更新为“已关闭”，则同步释放关联的 Pod 资源，防止后台热备池资源泄露
    if response.status_code == 200 and payload.get("status") == "closed":
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                release_resp = await client.post(
                    f"{settings.SCHEDULER_SERVICE_URL}/api/scheduler/pods/release", json={"case_id": case_id}
                )
                if release_resp.status_code == 200:
                    logger.info(f"Released pod for updated closed case {case_id}")
                else:
                    logger.warning(f"Pod release returned {release_resp.status_code} for updated case {case_id}")
        except Exception as e:
            logger.warning(
                event="pod_release_failed_on_update",
                message=f"编辑工单为关闭状态时 Pod 释放失败，case_id={case_id}",
                case_id=case_id,
                error=str(e),
                error_type=type(e).__name__,
            )
            POD_RELEASE_FAILURES_TOTAL.labels(reason=type(e).__name__).inc()

    return JSONResponse(content=response.json(), status_code=response.status_code)
