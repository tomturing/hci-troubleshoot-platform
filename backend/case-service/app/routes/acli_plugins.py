"""acli 插件管理端内部 API（只接受网关重签的 INTERNAL_API_TOKEN）。

职责边界：本文件只做 HTTP 包装与错误映射；规格校验、版本化、幂等与任务入队
在 ``app/services/acli_plugin_admin.py``，真正下发在 ``AcliPluginDeployRelay``。
页面从这里拿不到 node_ip / 口令 / 私钥 / bridge 地址。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field
from shared.database.postgres import DatabaseManager

from ..domain.acli_plugin_spec import (
    BUILTIN_PLUGINS,
    FORBIDDEN_SPEC_FIELDS,
    MANIFEST_REQUIRED_FIELDS,
    TEMPLATE_SPEC,
    SpecValidationError,
)
from ..security.auth import require_admin_token
from ..services.acli_plugin_admin import (
    AcliPluginAdminStore,
    AdminValidationError,
    prepare_publication,
)

admin_router = APIRouter(
    prefix="/internal/acli-plugins",
    tags=["acli-plugins-admin"],
    dependencies=[Depends(require_admin_token)],
)

database_manager: DatabaseManager | None = None


def set_database_manager(db_manager: DatabaseManager) -> None:
    """由 main.py 注入，与既有管理端路由保持同一范式。"""
    global database_manager
    database_manager = db_manager


def _store() -> AcliPluginAdminStore:
    if database_manager is None:
        raise HTTPException(status_code=500, detail="Database not initialized")
    return AcliPluginAdminStore(database_manager)


class PublishRequest(BaseModel):
    """页面提交体：只有插件规格 + 环境代号。"""

    spec: dict[str, Any]
    env_code: str = Field(default="24", max_length=32)


def _bad_request(errors: list[str]) -> HTTPException:
    return HTTPException(status_code=400, detail={"errors": errors})


@admin_router.get("/templates")
async def get_templates() -> dict[str, Any]:
    """新增插件信息模板：字段表 + 可导入 JSON + acli 契约常量。"""
    return {
        "template": TEMPLATE_SPEC,
        "manifest_required_fields": sorted(MANIFEST_REQUIRED_FIELDS),
        "builtin_plugins": sorted(BUILTIN_PLUGINS),
        "forbidden_fields": sorted(FORBIDDEN_SPEC_FIELDS),
    }


@admin_router.get("/environments")
async def list_environments() -> dict[str, Any]:
    """只返回环境代号/名称/启用状态/认证方式，不返回地址与凭据引用。"""
    return {"environments": await _store().list_environments()}


@admin_router.get("/plugins")
async def list_plugins() -> dict[str, Any]:
    return {"plugins": await _store().list_plugins()}


@admin_router.post("/plugins", status_code=202)
async def publish_plugin(
    body: PublishRequest,
    x_admin_user: str | None = Header(default=None, alias="X-Admin-User"),
) -> dict[str, Any]:
    """保存配置并自动下发；重复保存同一规格幂等返回原 deploy_id。"""
    try:
        prepared = prepare_publication(body.spec, env_code=body.env_code, actor=x_admin_user or "")
    except SpecValidationError as exc:
        raise _bad_request(exc.errors) from exc
    except AdminValidationError as exc:
        raise _bad_request(exc.errors) from exc
    return await _store().save_and_publish(prepared)


@admin_router.get("/plugins/{plugin_name}/deployments")
async def list_deployments(
    plugin_name: str,
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    return {"deployments": await _store().list_deployments(plugin_name, limit=limit)}


@admin_router.get("/deployments/{deploy_id}")
async def get_deployment(deploy_id: str) -> dict[str, Any]:
    deployment = await _store().get_deployment(deploy_id)
    if deployment is None:
        raise HTTPException(status_code=404, detail="下发任务不存在")
    return {"deployment": deployment}
