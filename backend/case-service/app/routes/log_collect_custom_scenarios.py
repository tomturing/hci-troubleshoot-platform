"""日志收集「自定义场景」（acli ``-c``）API。

两个入口，鉴权口径不同（与故障场景字典同构）：

- 客户读 ``GET /api/log-collect/custom-scenarios``：只返回**元数据**（id/名称/条数），
  永不下发 items —— 路径清单既是客户目录结构的信息泄露面，也是前端可篡改的注入面；
- 管理端 ``/internal/log-collect/custom-scenarios``：只接受 ``INTERNAL_API_TOKEN``，
  由网关的 admin 代理注入。

为什么创建任务时不复用这里的读接口：任务创建必须按 ``(scenario_id, revision)``
精确取历史版本，而客户读的是"最新 revision"，两者语义不同。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException
from shared.database.postgres import DatabaseManager
from shared.observability.logger import get_logger
from shared.observability.otel import get_current_trace_id
from sqlalchemy.exc import IntegrityError

from ..schemas.log_collect_custom_scenario import (
    CustomScenarioValidationIssue,
    CustomScenarioValidationResponse,
    LogCollectCustomScenarioClientItem,
    LogCollectCustomScenarioClientListResponse,
    LogCollectCustomScenarioCreate,
    LogCollectCustomScenarioListResponse,
    LogCollectCustomScenarioResponse,
    LogCollectCustomScenarioUpdate,
)
from ..security.auth import get_client_id, require_admin_token
from ..services.log_collect_custom_scenario_service import (
    CustomScenarioInUseError,
    CustomScenarioNotFoundError,
    LogCollectCustomScenarioService,
)
from ..services.log_collect_spc_catalog import load_catalog
from ..services.operation_audit import write_operation_audit

logger = get_logger("log-collect-custom-scenarios")

router = APIRouter(prefix="/api/log-collect", tags=["log-collect-custom-scenarios"])
admin_router = APIRouter(
    prefix="/internal/log-collect/custom-scenarios",
    tags=["log-collect-custom-scenarios-admin"],
    dependencies=[Depends(require_admin_token)],
)

database_manager: DatabaseManager | None = None


def set_database_manager(db_manager: DatabaseManager) -> None:
    global database_manager
    database_manager = db_manager


async def get_service() -> LogCollectCustomScenarioService:
    if not database_manager:
        raise HTTPException(status_code=500, detail="Database not initialized")
    async for session in database_manager.get_session():
        yield LogCollectCustomScenarioService(session)


def _not_found(code: str) -> HTTPException:
    return HTTPException(status_code=404, detail=f"自定义场景不存在：{code}")


async def _audit(action: str, code: str, details: dict, actor: str | None) -> None:
    """审计失败不影响业务（与既有口径一致）。"""
    if database_manager is None:
        return
    await write_operation_audit(
        database_manager.async_session_factory,
        action=action,
        resource_type="log_collect_custom_scenario",
        resource_id=code,
        details={**details, "updated_by": actor},
        trace_id=get_current_trace_id(),
    )


@router.get("/custom-scenarios", response_model=LogCollectCustomScenarioClientListResponse)
async def list_for_client(
    client_id: str = Depends(get_client_id),
    service: LogCollectCustomScenarioService = Depends(get_service),
) -> LogCollectCustomScenarioClientListResponse:
    """客户面板可选的自定义场景（只含启用项，且只有元数据）。"""
    rows = await service.list_latest(include_disabled=False)
    logger.info(
        event="log_collect_custom_scenario_read",
        message="客户读取自定义场景",
        client_id=client_id,
        count=len(rows),
        trace_id=get_current_trace_id(),
    )
    return LogCollectCustomScenarioClientListResponse(
        scenarios=[
            LogCollectCustomScenarioClientItem(
                scenario_id=row.scenario_id,
                revision=row.revision,
                label=row.label,
                summary=row.summary,
                item_count=row.item_count,
            )
            for row in rows
        ]
    )


@admin_router.get("", response_model=LogCollectCustomScenarioListResponse)
async def list_for_admin(
    service: LogCollectCustomScenarioService = Depends(get_service),
) -> LogCollectCustomScenarioListResponse:
    """管理端列表：含停用项与完整 items。"""
    return LogCollectCustomScenarioListResponse(scenarios=await service.list_latest(include_disabled=True))


@admin_router.get("/catalog")
async def get_spc_catalog() -> dict:
    """spc 清单目录：管理端编辑器勾选的数据源。

    读随镜像发布的快照而不是节点实时文件：管理员配置场景不该依赖某台机器在线；
    节点清单漂移由「从集群刷新」覆盖并用 sha256 对比。
    """
    return load_catalog()


@admin_router.post("", response_model=LogCollectCustomScenarioResponse, status_code=201)
async def create_scenario(
    payload: LogCollectCustomScenarioCreate,
    service: LogCollectCustomScenarioService = Depends(get_service),
    x_admin_user: str | None = Header(default=None, alias="X-Admin-User"),
) -> LogCollectCustomScenarioResponse:
    try:
        created = await service.create(payload, actor=x_admin_user)
    except IntegrityError as exc:
        raise HTTPException(status_code=409, detail=f"场景标识已存在：{payload.code}") from exc

    await _audit("log_collect.custom_scenario.create", payload.code, payload.model_dump(), x_admin_user)
    logger.info(
        event="log_collect_custom_scenario_write",
        message="新建自定义场景",
        code=payload.code,
        items=len(payload.items),
        updated_by=x_admin_user,
        trace_id=get_current_trace_id(),
    )
    return created


@admin_router.put("/{code}", response_model=LogCollectCustomScenarioResponse)
async def update_scenario(
    code: str,
    payload: LogCollectCustomScenarioUpdate,
    service: LogCollectCustomScenarioService = Depends(get_service),
    x_admin_user: str | None = Header(default=None, alias="X-Admin-User"),
) -> LogCollectCustomScenarioResponse:
    """更新会生成新 revision（不可原地覆盖，历史任务要能回溯）。"""
    try:
        updated = await service.update(code, payload, actor=x_admin_user)
    except CustomScenarioNotFoundError as exc:
        raise _not_found(code) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    await _audit(
        "log_collect.custom_scenario.update",
        code,
        {**payload.model_dump(exclude_unset=True), "revision": updated.revision},
        x_admin_user,
    )
    logger.info(
        event="log_collect_custom_scenario_write",
        message="更新自定义场景",
        code=code,
        revision=updated.revision,
        updated_by=x_admin_user,
        trace_id=get_current_trace_id(),
    )
    return updated


@admin_router.delete("/{code}", status_code=204)
async def delete_scenario(
    code: str,
    service: LogCollectCustomScenarioService = Depends(get_service),
    x_admin_user: str | None = Header(default=None, alias="X-Admin-User"),
) -> None:
    """仅"从未被任务引用"时允许删除；被引用过只能停用。"""
    try:
        await service.delete(code)
    except CustomScenarioInUseError as exc:
        raise HTTPException(
            status_code=409,
            detail="该场景已被历史任务引用，只能停用，不能删除",
        ) from exc
    await _audit("log_collect.custom_scenario.delete", code, {}, x_admin_user)


@admin_router.post("/validate", response_model=CustomScenarioValidationResponse)
async def validate_scenario(
    payload: LogCollectCustomScenarioCreate,
    service: LogCollectCustomScenarioService = Depends(get_service),
) -> CustomScenarioValidationResponse:
    """保存前试算：返回条数、下发字节数与真实序列化文本，不落库。

    编辑器需要在不产生副作用的前提下回答"这份配置多大、会不会超限、
    最终下发长什么样"，否则用户只能靠保存失败来发现。

    通道与条目不匹配（例如切回 -c 但残留 filters 条目）属于**可预期的用户错误**，
    必须返回 ok=false + issues 让编辑器就地提示，而不是抛 500：
    这类错误恰恰发生在管理员修改配置的过程中，报"服务器错误"会让人无从下手。
    """
    try:
        item_count, config_bytes, preview_text, preview_kind = await service.validate(payload)
    except ValueError as exc:
        return CustomScenarioValidationResponse(
            ok=False,
            issues=[CustomScenarioValidationIssue(field="items", message=str(exc))],
        )
    return CustomScenarioValidationResponse(
        ok=True,
        issues=[],
        item_count=item_count,
        config_bytes=config_bytes,
        preview_kind=preview_kind,
        preview_text=preview_text,
    )
