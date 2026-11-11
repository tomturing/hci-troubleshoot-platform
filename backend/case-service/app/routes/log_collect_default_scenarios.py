"""默认场景清单覆写 API。

客户侧只读预览（不含命令与过滤条件），管理端可编辑；与自定义场景相同，
管理端路径只接受网关注入的内部令牌。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException
from shared.database.postgres import DatabaseManager
from shared.observability.logger import get_logger
from shared.observability.otel import get_current_trace_id

from ..schemas.log_collect_default_scenario import (
    AdminDefaultScenarioListResponse,
    AdminDefaultScenarioManifest,
    ClientDefaultScenarioListResponse,
    ClientDefaultScenarioManifest,
    DefaultScenarioSave,
    DefaultScenarioValidationResponse,
)
from ..security.auth import get_client_id, require_admin_token
from ..services.log_collect_custom_scenario_service import serialize_spc
from ..services.log_collect_default_scenario_service import (
    DefaultScenarioError,
    LogCollectDefaultScenarioService,
    normalize_items,
)
from ..services.log_collect_spc_catalog import load_catalog
from ..services.operation_audit import write_operation_audit

logger = get_logger("log-collect-default-scenarios")

router = APIRouter(prefix="/api/log-collect", tags=["log-collect-default-scenarios"])
admin_router = APIRouter(
    prefix="/internal/log-collect/default-scenarios",
    tags=["log-collect-default-scenarios-admin"],
    dependencies=[Depends(require_admin_token)],
)

database_manager: DatabaseManager | None = None


def set_database_manager(db_manager: DatabaseManager) -> None:
    global database_manager
    database_manager = db_manager


async def get_service() -> LogCollectDefaultScenarioService:
    if not database_manager:
        raise HTTPException(status_code=500, detail="Database not initialized")
    async for session in database_manager.get_session():
        yield LogCollectDefaultScenarioService(session)


async def _audit(action: str, module: str, details: dict, actor: str | None) -> None:
    """审计失败不影响业务；调用链 ID 随审计落库，保证可追踪。"""
    if database_manager is None:
        return
    await write_operation_audit(
        database_manager.async_session_factory,
        action=action,
        resource_type="log_collect_default_scenario",
        resource_id=module,
        details={**details, "updated_by": actor},
        trace_id=get_current_trace_id(),
    )


@router.get("/default-scenario-manifests", response_model=ClientDefaultScenarioListResponse)
async def list_for_client(
    client_id: str = Depends(get_client_id),
    service: LogCollectDefaultScenarioService = Depends(get_service),
) -> ClientDefaultScenarioListResponse:
    """客户侧只读预览：路径清单可展示，但不下发可执行条目。"""
    rows = await service.list_manifests(full=False)
    return ClientDefaultScenarioListResponse(
        manifests=[ClientDefaultScenarioManifest(**row) for row in rows]
    )


@admin_router.get("", response_model=AdminDefaultScenarioListResponse)
async def list_for_admin(
    service: LogCollectDefaultScenarioService = Depends(get_service),
) -> AdminDefaultScenarioListResponse:
    rows = await service.list_manifests(full=True)
    return AdminDefaultScenarioListResponse(
        manifests=[AdminDefaultScenarioManifest(**row) for row in rows],
        sources=load_catalog()["sources"],
        total_items=sum(int(row["item_count"]) for row in rows),
    )


@admin_router.post("/validate", response_model=DefaultScenarioValidationResponse)
async def validate_manifest(payload: DefaultScenarioSave) -> DefaultScenarioValidationResponse:
    """保存前试算：不落库，失败返回可读原因而不是 500。"""
    try:
        normalized = normalize_items(payload.items)
        content, sha, size = serialize_spc(normalized)
    except (DefaultScenarioError, ValueError) as exc:
        return DefaultScenarioValidationResponse(ok=False, issues=[str(exc)])
    return DefaultScenarioValidationResponse(
        ok=True, item_count=len(normalized), sha256=sha, bytes=size, preview_text=content
    )


@admin_router.put("/{module}")
async def save_manifest(
    module: str,
    payload: DefaultScenarioSave,
    service: LogCollectDefaultScenarioService = Depends(get_service),
    x_admin_user: str | None = Header(default=None, alias="X-Admin-User"),
) -> dict[str, Any]:
    try:
        result = await service.save(module, payload.items, x_admin_user, get_current_trace_id())
    except DefaultScenarioError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await _audit("log_collect.default_scenario.save", module, {
        "revision": result["revision"], "item_count": result["item_count"], "sha256": result["sha256"],
    }, x_admin_user)
    logger.info(
        event="log_collect_default_scenario_write",
        message="保存默认场景清单覆写",
        module=module,
        revision=result["revision"],
        item_count=result["item_count"],
        sha256=result["sha256"],
        updated_by=x_admin_user,
        trace_id=get_current_trace_id(),
    )
    # 下发原文只在保存响应里出现一次，平台不留副本。
    result.pop("content", None)
    return result


@admin_router.delete("/{module}", status_code=204)
async def reset_manifest(
    module: str,
    service: LogCollectDefaultScenarioService = Depends(get_service),
    x_admin_user: str | None = Header(default=None, alias="X-Admin-User"),
) -> None:
    """恢复为该模块的镜像快照默认清单。"""
    try:
        changed = await service.reset(module)
    except DefaultScenarioError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await _audit("log_collect.default_scenario.reset", module, {"changed": changed}, x_admin_user)
    logger.info(
        event="log_collect_default_scenario_write",
        message="恢复默认场景清单",
        module=module,
        changed=changed,
        updated_by=x_admin_user,
        trace_id=get_current_trace_id(),
    )
