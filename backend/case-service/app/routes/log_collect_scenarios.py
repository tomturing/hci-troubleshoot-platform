"""日志收集「故障场景」字典 API。

两个入口，鉴权口径不同：

- 客户读（``GET /api/log-collect/scenarios``）：网关以客户身份签名后调用，只返回已启用项；
- 管理端读写（``/internal/log-collect/scenarios``）：只接受 ``Authorization: Bearer INTERNAL_API_TOKEN``，
  由网关的 admin 代理注入。

为什么客户读要单独一条而不是复用 ``/tasks{path}`` 代理：任务路由按工单归属做 BOLA 校验，
字典是平台级配置、与工单无关，混在任务前缀里会让权限判断含糊不清。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException
from shared.database.postgres import DatabaseManager
from shared.observability.logger import get_logger
from shared.observability.otel import get_current_trace_id
from sqlalchemy.exc import IntegrityError

from ..schemas.log_collect_scenario import (
    LogCollectScenarioCreate,
    LogCollectScenarioListResponse,
    LogCollectScenarioResponse,
    LogCollectScenarioUpdate,
)
from ..security.auth import get_client_id, require_admin_token
from ..services.log_collect_scenario_service import (
    LogCollectScenarioNotFoundError,
    LogCollectScenarioService,
)
from ..services.operation_audit import write_operation_audit

logger = get_logger("log-collect-scenarios")

router = APIRouter(prefix="/api/log-collect", tags=["log-collect-scenarios"])
admin_router = APIRouter(
    prefix="/internal/log-collect/scenarios",
    tags=["log-collect-scenarios-admin"],
    dependencies=[Depends(require_admin_token)],
)

database_manager: DatabaseManager | None = None


def set_database_manager(db_manager: DatabaseManager) -> None:
    """设置数据库管理器（由 main.py 调用）。"""
    global database_manager
    database_manager = db_manager


async def get_scenario_service() -> LogCollectScenarioService:
    if not database_manager:
        raise HTTPException(status_code=500, detail="Database not initialized")
    async for session in database_manager.get_session():
        yield LogCollectScenarioService(session)


def _not_found(value: str) -> HTTPException:
    return HTTPException(status_code=404, detail=f"场景字典项不存在：{value}")


async def _audit(action: str, value: str, details: dict, updated_by: str | None) -> None:
    """写一条审计；失败只记日志，不影响业务结果（与应用内既有口径一致）。"""
    if database_manager is None:
        return
    await write_operation_audit(
        database_manager.async_session_factory,
        action=action,
        resource_type="log_collect_scenario",
        resource_id=value,
        details={**details, "updated_by": updated_by},
        trace_id=get_current_trace_id(),
    )


@router.get("/scenarios", response_model=LogCollectScenarioListResponse)
async def list_scenarios_for_client(
    client_id: str = Depends(get_client_id),
    service: LogCollectScenarioService = Depends(get_scenario_service),
) -> LogCollectScenarioListResponse:
    """客户面板的场景字典（只含已启用项）。

    字典缺失不能让面板不可用：前端拿不到字典时会回落到 bridge 的短名，
    因此这里查空也只返回空数组，不报错。
    """
    scenarios = await service.list_scenarios(include_disabled=False)
    logger.info(
        event="log_collect_scenario_read",
        message="客户读取场景字典",
        client_id=client_id,
        count=len(scenarios),
        trace_id=get_current_trace_id(),
    )
    return LogCollectScenarioListResponse(scenarios=scenarios)


@admin_router.get("", response_model=LogCollectScenarioListResponse)
async def list_scenarios_for_admin(
    service: LogCollectScenarioService = Depends(get_scenario_service),
) -> LogCollectScenarioListResponse:
    """管理端列表：必须包含停用项，否则运营没有办法把停用的场景重新启用。"""
    scenarios = await service.list_scenarios(include_disabled=True)
    return LogCollectScenarioListResponse(scenarios=scenarios)


@admin_router.post("", response_model=LogCollectScenarioResponse, status_code=201)
async def create_scenario(
    payload: LogCollectScenarioCreate,
    service: LogCollectScenarioService = Depends(get_scenario_service),
    x_admin_user: str | None = Header(default=None, alias="X-Admin-User"),
) -> LogCollectScenarioResponse:
    try:
        created = await service.create_scenario(payload, updated_by=x_admin_user)
    except IntegrityError as exc:
        raise HTTPException(status_code=409, detail=f"场景已存在：{payload.value}") from exc

    await _audit("log_collect.scenario.create", payload.value, payload.model_dump(), x_admin_user)
    logger.info(
        event="log_collect_scenario_write",
        message="新建场景字典项",
        value=payload.value,
        updated_by=x_admin_user,
        trace_id=get_current_trace_id(),
    )
    return created


@admin_router.put("/{value}", response_model=LogCollectScenarioResponse)
async def update_scenario(
    value: str,
    payload: LogCollectScenarioUpdate,
    service: LogCollectScenarioService = Depends(get_scenario_service),
    x_admin_user: str | None = Header(default=None, alias="X-Admin-User"),
) -> LogCollectScenarioResponse:
    """按需更新；`value` 不可改（历史任务里存的就是它）。"""
    try:
        updated = await service.update_scenario(value, payload, updated_by=x_admin_user)
    except LogCollectScenarioNotFoundError as exc:
        raise _not_found(value) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    await _audit(
        "log_collect.scenario.update",
        value,
        payload.model_dump(exclude_unset=True),
        x_admin_user,
    )
    logger.info(
        event="log_collect_scenario_write",
        message="更新场景字典项",
        value=value,
        fields=sorted(payload.model_dump(exclude_unset=True)),
        updated_by=x_admin_user,
        trace_id=get_current_trace_id(),
    )
    return updated
