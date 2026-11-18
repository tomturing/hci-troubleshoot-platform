"""acli 插件 API：管理端规格维护 + 阶段二（票据拉取 manifest / 下载包）。

职责边界：本文件只做 HTTP 包装与错误映射；规格校验与版本化在
``app/services/acli_plugin_admin.py``。**插件到达节点不由服务端发起**：旧下发
链路（部署命令随响应下发、浏览器执行回报、任务轮询）已随链路收口删除（D1），
节点收敛改由客户侧 acli_sync 第二阶段持票拉取 manifest 驱动（S4c）。
页面从这里拿不到口令 / 私钥 / bridge 地址，也拿不到任何部署任务。

三族路由、三种信任级别：

* ``admin_router``（``/internal/acli-plugins``）：管理令牌，规格读写；
* ``client_router``（``/api/acli-plugins``）：客户身份（网关重签），只用于取票；
* ``sync_router``（``/internal/acli-plugins``）：**只认票据**，不认管理令牌也不认
  客户身份——票据是唯一凭据，fail-closed 401，桥拿不到任何长期密钥。
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel
from shared.database.postgres import DatabaseManager
from shared.observability.logger import get_logger

from ..config import settings
from ..domain.acli_plugin_spec import (
    BUILTIN_PLUGINS,
    FORBIDDEN_SPEC_FIELDS,
    MANIFEST_REQUIRED_FIELDS,
    TEMPLATE_SPEC,
    SpecValidationError,
    build_descriptor,
    compile_descriptor,
)
from ..security.auth import get_client_id, require_admin_token, verify_case_exists
from ..services.acli_plugin_admin import (
    AcliPluginAdminStore,
    AdminValidationError,
    prepare_spec,
)
from ..services.plugin_ticket import PluginTicketError, issue_plugin_ticket, verify_plugin_ticket

logger = get_logger("acli-plugins")

admin_router = APIRouter(
    prefix="/internal/acli-plugins",
    tags=["acli-plugins-admin"],
    dependencies=[Depends(require_admin_token)],
)

# 阶段二数据面：与管理端同前缀但**不继承**管理令牌依赖（桥不持有管理凭证）。
sync_router = APIRouter(prefix="/internal/acli-plugins", tags=["acli-plugins-sync"])

# 客户面：custom-ui 取票；走网关 ``require_user`` + 身份重签，与日志收集同族。
client_router = APIRouter(prefix="/api/acli-plugins", tags=["acli-plugins-client"])

# 包对象名：内容寻址（plugin_name + spec_hash），不接受任何用户输入的路径片段。
_PACKAGE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

database_manager: DatabaseManager | None = None


def set_database_manager(db_manager: DatabaseManager) -> None:
    """由 main.py 注入，与既有管理端路由保持同一范式。"""
    global database_manager
    database_manager = db_manager


def _store() -> AcliPluginAdminStore:
    if database_manager is None:
        raise HTTPException(status_code=500, detail="Database not initialized")
    return AcliPluginAdminStore(database_manager)


class SaveSpecRequest(BaseModel):
    """页面提交体：只有插件规格。

    目标会话/环境字段已随 D1 删除——插件何时、到达哪些节点，完全由客户侧
    acli_sync 触发驱动，管理员只维护规格事实源。
    """

    spec: dict[str, Any]


def _bad_request(errors: list[str]) -> HTTPException:
    return HTTPException(status_code=400, detail={"errors": errors})


async def get_session():
    """客户面取票要查工单存在性，与既有路由同一会话依赖范式。"""
    if database_manager is None:
        raise HTTPException(status_code=500, detail="Database not initialized")
    async for session in database_manager.get_session():
        yield session


def _unauthorized(reason: str) -> HTTPException:
    return HTTPException(status_code=401, detail=f"票据无效：{reason}")


def _package_path(plugin_name: str, spec_hash: str) -> Path:
    """插件包对象路径：内容寻址，单一真相（manifest 与下载端点共用本函数）。

    只由 ``plugin_name`` + ``spec_hash`` 拼成，不接受调用方给定的路径片段；
    ``plugin_name`` 来自库内已校验过的插件名，``spec_hash`` 来自哈希输出。
    """
    root = Path(settings.ACLI_PLUGIN_PACKAGE_ROOT).resolve()
    return root / f"{plugin_name}-{spec_hash}.tar"


def _package_object(plugin_name: str, spec_hash: str) -> tuple[Path | None, str]:
    """返回 ``(可读包文件, 原因)``；没包时不建目录、不静默降级。"""
    if not str(settings.ACLI_PLUGIN_PACKAGE_ROOT).strip():
        return None, "package_source_undefined"
    path = _package_path(plugin_name, spec_hash)
    if not path.is_file():
        return None, "package_not_found"
    return path, ""


def _require_plugin_ticket(
    case_id: str,
    plugin_ticket: str,
) -> None:
    """票据校验：缺票/伪造/过期/串用 other case 一律 401（fail-closed）。"""
    if not case_id or not plugin_ticket:
        raise _unauthorized("缺少 X-Plugin-Ticket 或 case_id")
    try:
        verify_plugin_ticket(case_id, plugin_ticket)
    except PluginTicketError as exc:
        logger.warning(
            event="acli_plugin_ticket_rejected",
            message=str(exc),
            case_id=case_id,
        )
        raise _unauthorized(str(exc)) from exc


@admin_router.get("/templates")
async def get_templates() -> dict[str, Any]:
    """新增插件信息模板：字段表 + 可导入 JSON + acli 契约常量。"""
    return {
        "template": TEMPLATE_SPEC,
        "manifest_required_fields": sorted(MANIFEST_REQUIRED_FIELDS),
        "builtin_plugins": sorted(BUILTIN_PLUGINS),
        "forbidden_fields": sorted(FORBIDDEN_SPEC_FIELDS),
    }


@admin_router.get("/plugins")
async def list_plugins() -> dict[str, Any]:
    return {"plugins": await _store().list_plugins()}


@admin_router.post("/specs", status_code=201)
async def save_spec(
    body: SaveSpecRequest,
    x_admin_user: str | None = Header(default=None, alias="X-Admin-User"),
) -> dict[str, Any]:
    """仅保存规格：校验 → 不可变 revision → 更新插件档案；不产生任何下发副作用。

    同一规格重复保存按 ``spec_hash`` 幂等（不重复增长 revision），回执 ``saved``。
    """
    try:
        prepared = prepare_spec(body.spec, actor=x_admin_user or "")
    except SpecValidationError as exc:
        raise _bad_request(exc.errors) from exc
    except AdminValidationError as exc:
        raise _bad_request(exc.errors) from exc
    return await _store().save_spec(prepared)


# ── 阶段二：取票 + 拉 manifest + 下载包（S4c）─────────────────────────────────


class IssueTicketRequest(BaseModel):
    """取票请求：只带工单号（与可选 TTL）；不接受任何插件范围参数。"""

    case_id: str
    ttl_seconds: int | None = None


@client_router.post("/ticket")
async def issue_ticket(
    body: IssueTicketRequest,
    client_id: str = Depends(get_client_id),
    session=Depends(get_session),
) -> dict[str, Any]:
    """按 case 签发短期只读票据，供 custom-ui 随 ``acli_sync`` 透传给桥。

    只校工单存在（不校归属），与日志收集同口径：插件 manifest 不是工单业务数据，
    把“工单由别人建”变成阶段二静默不执行更难排查。工单不存在仍 404。
    """
    case_id = body.case_id.strip()
    if not case_id:
        raise HTTPException(status_code=400, detail="case_id 不能为空")
    await verify_case_exists(case_id, session)
    token, expires_at = issue_plugin_ticket(case_id, body.ttl_seconds)
    logger.info(
        event="acli_plugin_ticket_issued",
        case_id=case_id,
        client_id=client_id,
        ticket_id=hashlib.sha256(token.encode()).hexdigest()[:12],
        expires_at=expires_at,
    )
    return {"ticket": token, "expires_at": expires_at, "case_id": case_id}


def _as_dict(value: Any) -> dict[str, Any]:
    """jsonb 回值容错：asyncpg 默认已经解成 dict，驱动/方言变了也不会默默当空处理。"""
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            loaded = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return loaded if isinstance(loaded, dict) else {}
    return {}


def _entry_of(plugin_name: str, spec_hash: str, validation: Any, spec_json: Any) -> dict[str, Any]:
    """把一条期望 revision 拼成 manifest 插件项；compiled 丢失时才回退重算。

    回退重算会让 ``register_item.last_updated_at`` 每次不同（退化成全量重装），
    所以必须留痕；compiled 固化上线后新保存的 revision 不会再走这条路。
    """
    validation = _as_dict(validation)
    compiled = validation.get("compiled")
    if not isinstance(compiled, dict) or not compiled.get("files"):
        logger.warning(
            event="acli_plugin_compiled_missing",
            message="revision 未固化编译产物，本次按旧口径重算",
            plugin_name=plugin_name,
            spec_hash=spec_hash,
        )
        compiled = compile_descriptor(build_descriptor(_as_dict(spec_json)))
    package_path, _ = _package_object(plugin_name, spec_hash)
    sidecar = Path(f"{package_path}.sha256") if package_path is not None else None
    return {
        "name": plugin_name,
        "version": str(compiled.get("version") or ""),
        "spec_hash": spec_hash,
        "update_mode": str(compiled.get("update_mode") or ""),
        "built_in": bool(compiled.get("built_in")),
        "smoke_args": str(compiled.get("smoke_args") or ""),
        "enable_completion": bool(compiled.get("enable_completion")),
        "size": int(compiled.get("size") or 0),
        "files": list(compiled.get("files") or []),
        "register_item": dict(compiled.get("register_item") or {}),
        # 包通道：没落包时 package_ref 为空，桥必须走内联 files 收敛；
        # 有包但无摘要 sidecar 时 sha256 为空，桥不得拆包（无法完整性校验）。
        "package_ref": package_path.name if package_path is not None else "",
        "sha256": sidecar.read_text(encoding="utf-8").strip()[:64] if sidecar and sidecar.is_file() else "",
    }


@sync_router.get("/manifest")
async def get_manifest(
    case_id: str = Query(default="", description="票据绑定的工单号"),
    x_plugin_ticket: str = Header(default="", alias="X-Plugin-Ticket"),
) -> dict[str, Any]:
    """返回本 case 的插件期望态（admin-ui 保存的不可变 revision 编译产物）。

    只读、无副作用；不读其它工单数据。每次拉取落平台审计日志。
    """
    _require_plugin_ticket(case_id, x_plugin_ticket)
    rows = await _store().list_desired()
    plugins = []
    for row in rows:
        spec_hash = str(row.get("desired_spec_hash") or "")
        if not spec_hash or not row.get("spec_json"):
            # 档案存在但无期望 revision（历史脏数据）：不猜、不补，直接跳过。
            logger.warning(
                event="acli_plugin_manifest_incomplete",
                message="插件无可用期望 revision，已从 manifest 跳过",
                plugin_name=row.get("plugin_name"),
            )
            continue
        plugins.append(
            _entry_of(
                str(row["plugin_name"]),
                spec_hash,
                row.get("validation"),
                row.get("spec_json"),
            )
        )
    logger.info(
        event="acli_plugin_manifest_served",
        case_id=case_id,
        ticket_id=hashlib.sha256(x_plugin_ticket.encode()).hexdigest()[:12],
        plugin_count=len(plugins),
    )
    return {"manifest_version": "2.0", "case_id": case_id, "plugins": plugins}


@sync_router.get("/package/{plugin_name}")
async def get_package(
    plugin_name: str,
    case_id: str = Query(default=""),
    spec_hash: str = Query(default=""),
    x_plugin_ticket: str = Header(default="", alias="X-Plugin-Ticket"),
) -> FileResponse:
    """票据鉴权的包下载：从共享卷 objects/ 流式直出（不引入签名 URL）。

    存储未配置或包不存在 → 404 带区分性原因码，桥据此回退内联文件收敛，
    而不是拿到一个截断的空文件当成功。
    """
    _require_plugin_ticket(case_id, x_plugin_ticket)
    if not _PACKAGE_NAME_RE.match(plugin_name) or not re.fullmatch(r"[0-9a-f]{64}", spec_hash or ""):
        raise HTTPException(status_code=404, detail={"code": "package_not_found", "plugin_name": plugin_name})
    path, reason = _package_object(plugin_name, spec_hash)
    if path is None:
        raise HTTPException(status_code=404, detail={"code": reason, "plugin_name": plugin_name})
    logger.info(
        event="acli_plugin_package_served",
        case_id=case_id,
        plugin_name=plugin_name,
        spec_hash=spec_hash,
        size=path.stat().st_size,
    )
    return FileResponse(path, media_type="application/octet-stream", filename=path.name)
