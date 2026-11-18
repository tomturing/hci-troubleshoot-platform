"""acli 插件管理端内部 API（只接受网关重签的 INTERNAL_API_TOKEN）。

职责边界：本文件只做 HTTP 包装与错误映射；规格校验、版本化与幂等在
``app/services/acli_plugin_admin.py``。**执行不由服务端发起**：服务端只返回部署命令，
由持有 SSH 会话的浏览器在桥连接上执行并回报结果（与 acli_sync / log_collect 同一模型）。
页面从这里拿不到口令 / 私钥 / bridge 地址。
"""

from __future__ import annotations

import uuid
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
    parse_deploy_result,
)
from ..security.auth import require_admin_token
from ..services.acli_plugin_admin import (
    AcliPluginAdminStore,
    AdminValidationError,
    prepare_publication,
)

# 单条部署命令的执行上限：设备侧脚本自带 720s 预算，浏览器侧留同样余量。
ACLI_PLUGIN_COMMAND_TIMEOUT_SECONDS = 720

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


class DeployReportRequest(BaseModel):
    """浏览器执行回执；字段只允许执行结果，不允许携带凭据或桥地址。"""

    exec_id: str = ""
    exit_code: int = -1
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    duration_ms: int = 0
    bridge_instance_id: str = ""


class PublishRequest(BaseModel):
    """页面提交体：插件规格 + 已登录 SSH 会话（由 bridge 会话列表选择）。

    目标不是静态环境表：case_id + node_ip 必须来自 custom-ui + terminal_bridge
    当前已登录的会话；服务端不接收也不保存任何 SSH 凭据。
    """

    spec: dict[str, Any]
    case_id: str = Field(default="", max_length=128)
    node_ip: str = Field(default="", max_length=64)
    # acli 重装会清空插件目录，而节点当前态无法用服务端幂等表达。
    # force=True 跳过"succeeded 且 current_spec_hash 对齐"的幂等命中，
    # 让管理员在节点被清空后能把同一规格重新发下去。
    force: bool = False


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
    """兼容旧端点：静态环境白名单已废弃，恒为空；请用 /sessions。"""
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
        if not body.case_id.strip() or not body.node_ip.strip():
            raise _bad_request(["请先从 bridge 已登录会话列表选择目标环境"])
        prepared = prepare_publication(
            body.spec,
            env_code=body.case_id.strip(),
            actor=x_admin_user or "",
        )
        prepared["target_case_id"] = body.case_id.strip()
        prepared["target_node_ip"] = body.node_ip.strip()
        prepared["force"] = body.force
        prepared["exec_id"] = uuid.uuid4().hex
        prepared["trace_id"] = uuid.uuid4().hex
    except SpecValidationError as exc:
        raise _bad_request(exc.errors) from exc
    except AdminValidationError as exc:
        raise _bad_request(exc.errors) from exc
    result = await _store().save_and_publish(prepared)
    exec_id = prepared["exec_id"]
    trace_id = prepared["trace_id"]
    if result.get("idempotent"):
        # 幂等命中可能是「另一个浏览器正在执行」：复用该任务已有的 exec_id，
        # 让本次页面也能接着执行并用同一 exec_id 回报，避免回执被 409 拒掉。
        existing = await _store().get_deployment(result["deploy_id"])
        if existing:
            exec_id = str(existing.get("exec_id") or exec_id)
            trace_id = str(existing.get("trace_id") or trace_id)
            result["status"] = str(existing.get("status") or result.get("status") or "")
    # 部署命令随响应下发，由浏览器在自己的桥连接上逐条执行；服务端不连桥。
    result["plan"] = {
        "exec_id": exec_id,
        "trace_id": trace_id,
        "commands": prepared["deploy_commands"],
        "timeout_seconds": ACLI_PLUGIN_COMMAND_TIMEOUT_SECONDS,
        "case_id": prepared["target_case_id"],
        "node_ip": prepared["target_node_ip"],
    }
    return result


@admin_router.post("/deployments/{deploy_id}/result")
async def report_deploy_result(
    deploy_id: str,
    body: DeployReportRequest,
    x_admin_user: str | None = Header(default=None, alias="X-Admin-User"),
) -> dict[str, Any]:
    """浏览器执行完部署命令后回报结果；服务端解析设备侧 ACP_RESULT 后落终态。

    服务端不再能自行观测执行过程，因此回执必须带 ``exec_id`` 防串单，
    并以设备侧脚本输出的 spec_hash 与自检结论为准（页面无法伪造生效）。
    """
    deployment = await _store().get_deployment(deploy_id)
    if deployment is None:
        raise HTTPException(status_code=404, detail="下发任务不存在")
    if deployment.get("status") in ("succeeded", "failed"):
        return {"deployment": deployment, "idempotent": True}
    if deployment.get("exec_id") and body.exec_id and body.exec_id != deployment["exec_id"]:
        raise HTTPException(status_code=409, detail="exec_id 与下发任务不一致")

    verification = parse_deploy_result(body.stdout, exit_code=int(body.exit_code))
    verification["spec_hash"] = deployment["spec_hash"]
    verification["exec_id"] = deployment["exec_id"]
    verification["trace_id"] = deployment["trace_id"]
    verification["reported_by"] = x_admin_user or "browser"
    bridge_result = {
        "exit_code": int(body.exit_code),
        "timed_out": bool(body.timed_out),
        "duration_ms": int(body.duration_ms),
        "bridge_instance_id": body.bridge_instance_id[:128],
        "stderr_tail": (body.stderr or "")[-2000:],
    }
    expected = str(deployment.get("spec_hash") or "")
    if expected and verification.get("device_spec_hash") != expected:
        updated = await _store().finish_reported(
            deploy_id,
            status="failed",
            verification=verification,
            bridge_result=bridge_result,
            error_code="spec_hash_mismatch",
            error_message="设备侧回执的 spec_hash 与下发任务不一致",
        )
        return {"deployment": updated or deployment, "idempotent": False}
    if not verification.get("ok"):
        updated = await _store().finish_reported(
            deploy_id,
            status="failed",
            verification=verification,
            bridge_result=bridge_result,
            error_code=str(verification.get("device_error") or "verification_failed"),
            error_message="设备侧自检未通过（status/退出码/冒烟输出）",
        )
        return {"deployment": updated or deployment, "idempotent": False}
    updated = await _store().finish_reported(
        deploy_id,
        status="succeeded",
        verification=verification,
        bridge_result=bridge_result,
        error_code="",
        error_message="",
    )
    await _store().mark_plugin_succeeded(str(deployment["plugin_name"]), expected)
    return {"deployment": updated or deployment, "idempotent": False}


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
