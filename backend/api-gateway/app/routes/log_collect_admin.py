"""日志收集「故障场景」字典的管理端代理。

职责边界：
- 本路由只做两件事：**管理端鉴权**（require_admin，JWT 验签）与**内部令牌重签**，
  业务校验（字段合法性、value 不可改、停用语义）全部在 case-service；
- 为什么不复用 ``/api/log-collect`` 的前缀代理：那条代理把客户身份（X-Client-ID）
  重签给下游，而字典维护是平台运营动作、不属于任何客户，两条身份链路不能混用。

审计与追溯：管理端前端在 ``X-Admin-User`` 里带上操作者，原样透传给 case-service
落 ``updated_by`` 与 operation_audit；缺失时下游记 NULL，不编造身份。
"""

from typing import Any
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from shared.observability.logger import get_logger

from app.config import settings
from app.security.gateway_auth import require_admin

router = APIRouter(
    prefix="/api/v1/log-collect-scenarios",
    tags=["log-collect-scenarios-admin"],
    dependencies=[Depends(require_admin)],
)
logger = get_logger("gateway-log-collect-scenarios")

TIMEOUT_SECONDS = 15.0
UPSTREAM_PREFIX = "/internal/log-collect/scenarios"


def _internal_headers(request: Request) -> dict[str, str]:
    """内部令牌 + 操作者透传（操作者仅用于审计留痕，不参与鉴权）。

    操作者来源优先级：前端显式 `X-Admin-User` → 网关验签后的 JWT ``sub``。
    后者是服务端可信身份，前端没传时用它兜底，避免 `updated_by` 全部落空；
    两者都没有（例如共享令牌调用的脚本）时留空，不编造身份。
    """
    token = settings.INTERNAL_API_TOKEN
    if not token or not str(token).strip():
        raise HTTPException(status_code=500, detail="INTERNAL_API_TOKEN 未配置，无法调用内部服务")
    headers = {"Authorization": f"Bearer {str(token).strip()}"}
    admin_user = request.headers.get("x-admin-user")
    if not admin_user:
        actor = getattr(request.state, "auth", None)
        admin_user = getattr(actor, "user_id", None)
    if admin_user:
        headers["X-Admin-User"] = str(admin_user)
    return headers


async def _request_upstream(
    method: str,
    path: str,
    request: Request,
    payload: Any | None = None,
    *,
    prefix: str | None = None,
    query: str | None = None,
) -> Response:
    target_prefix = prefix or UPSTREAM_PREFIX
    url = f"{settings.CASE_SERVICE_URL.rstrip('/')}{target_prefix}{path}"
    # query 已经是 URL 编码后的原始串，原样透传即可；重新编码会把 %20 变成 %2520。
    if query:
        url = f"{url}?{query}"
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
            upstream = await client.request(method, url, json=payload, headers=_internal_headers(request))
    except httpx.RequestError as exc:
        logger.error(event="log_collect_scenario_admin_proxy_error", message=str(exc), path=path, method=method)
        raise HTTPException(status_code=503, detail="Case Service unavailable") from exc

    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers={"content-type": upstream.headers.get("content-type", "application/json")},
    )


@router.get("")
async def list_scenarios(request: Request) -> Response:
    """管理端列表（含停用项）。"""
    return await _request_upstream("GET", "", request)


@router.post("", status_code=201)
async def create_scenario(request: Request) -> Response:
    payload = await request.json()
    return await _request_upstream("POST", "", request, payload)


@router.put("/{value}")
async def update_scenario(value: str, request: Request) -> Response:
    payload = await request.json()
    return await _request_upstream("PUT", f"/{value}", request, payload)


# ── 自定义场景（acli -c）管理端 ─────────────────────────────────────────────
#
# 与故障场景字典分开一个前缀：两者的数据模型与安全边界不同
# （字典是平台文案，自定义场景含客户目录清单），混在一条路由里
# 会让"哪些字段可以下发给客户"这种判断无处安放。
custom_router = APIRouter(
    prefix="/api/v1/log-collect-custom-scenarios",
    tags=["log-collect-custom-scenarios-admin"],
    dependencies=[Depends(require_admin)],
)
CUSTOM_UPSTREAM_PREFIX = "/internal/log-collect/custom-scenarios"


@custom_router.get("")
async def list_custom_scenarios(request: Request) -> Response:
    return await _request_upstream("GET", "", request, prefix=CUSTOM_UPSTREAM_PREFIX)


@custom_router.post("", status_code=201)
async def create_custom_scenario(request: Request) -> Response:
    payload = await request.json()
    return await _request_upstream("POST", "", request, payload, prefix=CUSTOM_UPSTREAM_PREFIX)


@custom_router.get("/catalog")
async def get_spc_catalog(request: Request) -> Response:
    """spc 清单快照（只读）：管理端编辑器的勾选数据源。

    返回的 11 份清单随镜像发布，不依赖任何节点在线；条目自带
    ``usable_by_config`` 标注，前端据此决定 -c 通道下哪些条目可勾选。
    """
    return await _request_upstream("GET", "/catalog", request, prefix=CUSTOM_UPSTREAM_PREFIX)


@custom_router.post("/validate")
async def validate_custom_scenario(request: Request) -> Response:
    """保存前试算（条数/字节数）；不落库，因此不写审计。"""
    payload = await request.json()
    return await _request_upstream("POST", "/validate", request, payload, prefix=CUSTOM_UPSTREAM_PREFIX)


@custom_router.put("/{code}")
async def update_custom_scenario(code: str, request: Request) -> Response:
    payload = await request.json()
    return await _request_upstream("PUT", f"/{code}", request, payload, prefix=CUSTOM_UPSTREAM_PREFIX)


@custom_router.delete("/{code}", status_code=204)
async def delete_custom_scenario(code: str, request: Request) -> Response:
    return await _request_upstream("DELETE", f"/{code}", request, prefix=CUSTOM_UPSTREAM_PREFIX)


# ── 默认场景清单（按模块覆写）管理端 ───────────────────────────────────────
# 与自定义场景分开前缀：一个是用户创建的场景，一个是平台默认模块清单，
# 删除语义也完全不同（前者停用，后者恢复为镜像快照）。
default_router = APIRouter(
    prefix="/api/v1/log-collect-default-scenarios",
    tags=["log-collect-default-scenarios-admin"],
    dependencies=[Depends(require_admin)],
)
DEFAULT_UPSTREAM_PREFIX = "/internal/log-collect/default-scenarios"


@default_router.get("")
async def list_default_manifests(request: Request) -> Response:
    return await _request_upstream("GET", "", request, prefix=DEFAULT_UPSTREAM_PREFIX)


@default_router.post("/validate")
async def validate_default_manifest(request: Request) -> Response:
    payload = await request.json()
    return await _request_upstream("POST", "/validate", request, payload, prefix=DEFAULT_UPSTREAM_PREFIX)


@default_router.put("/{module}")
async def save_default_manifest(module: str, request: Request) -> Response:
    payload = await request.json()
    return await _request_upstream("PUT", f"/{module}", request, payload, prefix=DEFAULT_UPSTREAM_PREFIX)


@default_router.delete("/{module}", status_code=204)
async def reset_default_manifest(module: str, request: Request) -> Response:
    return await _request_upstream("DELETE", f"/{module}", request, prefix=DEFAULT_UPSTREAM_PREFIX)


# ── 日志上传（管理端跨工单只读）───────────────────────────────────────────
#
# 与客户身份链路彻底分开：客户路由把 X-Client-ID 重签给下游且强制 case_id，
# 这里只允许 admin JWT + 内部令牌，下游用 require_admin_token 再验一次。
# 只挂 GET，管理端没有任何写路径（已确认：不允许重新核验/终止/重传）。
tasks_router = APIRouter(
    prefix="/api/v1/log-collect/admin/tasks",
    tags=["log-collect-admin-tasks"],
    dependencies=[Depends(require_admin)],
)
TASKS_UPSTREAM_PREFIX = "/internal/log-collect/admin/tasks"


@tasks_router.get("")
async def list_admin_tasks(request: Request) -> Response:
    """跨工单分页列表 + 汇总条；筛选参数原样透传给 case-service 解析。"""
    return await _request_upstream(
        "GET",
        "",
        request,
        prefix=TASKS_UPSTREAM_PREFIX,
        query=request.url.query,
    )


def _safe_id(value: str) -> str:
    """路径段只接受规范化 UUID，避免用户输入拼进上游 URL 造成路径穿越。"""
    try:
        return str(UUID(value))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="非法 ID") from exc


@tasks_router.get("/{task_id}/destination-progress")
async def get_destination_progress(task_id: str, request: Request) -> Response:
    """日志服务器真实进度（P1 按产物名 / P2 按 relay_upload_id），只读。"""
    return await _request_upstream(
        "GET",
        f"/{_safe_id(task_id)}/destination-progress",
        request,
        prefix=TASKS_UPSTREAM_PREFIX,
    )


# ── 证据端点（详情抽屉四页签）─────────────────────────────────────────────
# 只做鉴权 + URL 转发：结论、体积、速率、分桶全部由 case-service 计算，
# 网关不得再拼一份口径（168/146 对不上账的根因就是多处各自计算）。


@tasks_router.get("/{task_id}/transfer-metrics")
async def get_transfer_metrics(task_id: str, request: Request) -> Response:
    return await _request_upstream(
        "GET",
        f"/{_safe_id(task_id)}/transfer-metrics",
        request,
        prefix=TASKS_UPSTREAM_PREFIX,
        query=request.url.query,
    )


@tasks_router.get("/{task_id}/network-probe")
async def get_network_probe(task_id: str, request: Request) -> Response:
    return await _request_upstream(
        "GET",
        f"/{_safe_id(task_id)}/network-probe",
        request,
        prefix=TASKS_UPSTREAM_PREFIX,
    )


@tasks_router.get("/{task_id}/artifact-manifest")
async def get_artifact_manifest(task_id: str, request: Request) -> Response:
    return await _request_upstream(
        "GET",
        f"/{_safe_id(task_id)}/artifact-manifest",
        request,
        prefix=TASKS_UPSTREAM_PREFIX,
    )


@tasks_router.get("/{task_id}/process-logs")
async def get_process_logs(task_id: str, request: Request) -> Response:
    return await _request_upstream(
        "GET",
        f"/{_safe_id(task_id)}/process-logs",
        request,
        prefix=TASKS_UPSTREAM_PREFIX,
    )


@tasks_router.get("/{task_id}/process-logs/{log_id}/content")
async def get_process_log_content(task_id: str, log_id: str, request: Request) -> Response:
    return await _request_upstream(
        "GET",
        f"/{_safe_id(task_id)}/process-logs/{_safe_id(log_id)}/content",
        request,
        prefix=TASKS_UPSTREAM_PREFIX,
    )


@tasks_router.get("/{task_id}")
async def get_admin_task(task_id: str, request: Request) -> Response:
    """单任务详情；放在最后注册，避免吃掉上面更具体的子路径。"""
    return await _request_upstream(
        "GET",
        f"/{_safe_id(task_id)}",
        request,
        prefix=TASKS_UPSTREAM_PREFIX,
    )
