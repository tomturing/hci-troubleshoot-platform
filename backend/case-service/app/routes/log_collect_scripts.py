"""日志收集「收集脚本」库管理 API（仅管理端）。

为什么全用 query/body 而不是 ``/{code}`` 路径参数：``code`` 是节点上的文件名，
基线里包含子目录条目（如 ``tools/vs_calc_mem.py``），路径参数无法表达斜杠；
统一走 query 后，前端不需要为「文件名里有点」或「在子目录里」写特例。

权限与默认场景一致：只有带网关注入内部令牌的管理端请求能进来。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from shared.database.postgres import DatabaseManager
from shared.observability.logger import get_logger
from shared.observability.otel import get_current_trace_id

from ..schemas.log_collect_script import (
    LogCollectScriptCreateRequest,
    LogCollectScriptDetail,
    LogCollectScriptListResponse,
    LogCollectScriptReferenceListResponse,
    LogCollectScriptRevisionDetail,
    LogCollectScriptRevisionListResponse,
    LogCollectScriptSummary,
    LogCollectScriptValidateRequest,
    LogCollectScriptValidationResponse,
)
from ..security.auth import require_admin_token
from ..services.log_collect_script_service import (
    LogCollectScriptService,
    ScriptError,
    baseline_scripts,
    baseline_source,
    infer_interpreter,
    normalize_content,
    sha256_of,
    validate_code,
)
from ..services.operation_audit import write_operation_audit

logger = get_logger("log-collect-scripts")

router = APIRouter(
    prefix="/internal/log-collect/scripts",
    tags=["log-collect-scripts-admin"],
    dependencies=[Depends(require_admin_token)],
)

database_manager: DatabaseManager | None = None


def set_database_manager(db_manager: DatabaseManager) -> None:
    global database_manager
    database_manager = db_manager


async def get_service() -> LogCollectScriptService:
    if not database_manager:
        raise HTTPException(status_code=500, detail="Database not initialized")
    async for session in database_manager.get_session():
        yield LogCollectScriptService(session)


def _http_error(exc: ScriptError) -> HTTPException:
    status = {"not_found": 404, "conflict": 409}.get(exc.kind, 422)
    return HTTPException(status_code=status, detail=str(exc))


async def _audit(action: str, code: str, details: dict[str, Any], actor: str | None) -> None:
    if database_manager is None:
        return
    await write_operation_audit(
        database_manager.async_session_factory,
        action=action,
        resource_type="log_collect_script",
        resource_id=code,
        details={**details, "updated_by": actor},
        trace_id=get_current_trace_id(),
    )


def _log(event: str, message: str, **kwargs: Any) -> None:
    logger.info(event=event, message=message, trace_id=get_current_trace_id(), **kwargs)


@router.get("", response_model=LogCollectScriptListResponse)
async def list_scripts(service: LogCollectScriptService = Depends(get_service)) -> LogCollectScriptListResponse:
    """列表：平台默认快照 + 覆写/新增合并，不含正文（正文单独取，避免列表几百 KB）。"""
    rows = await service.list_scripts()
    return LogCollectScriptListResponse(
        scripts=[LogCollectScriptSummary(**row) for row in rows],
        total=len(rows),
        baseline_total=len(baseline_scripts()),
        baseline_source=baseline_source(),
    )


@router.get("/detail", response_model=LogCollectScriptDetail)
async def get_script(
    code: str = Query(min_length=1, max_length=128),
    revision: int | None = Query(default=None, ge=1),
    service: LogCollectScriptService = Depends(get_service),
) -> LogCollectScriptDetail:
    rows = await service.list_scripts()
    row = next((item for item in rows if item["code"] == code), None)
    if row is None:
        raise HTTPException(status_code=404, detail=f"脚本不存在：{code}")
    content = await service.content_of(code, revision)
    if content is None:
        raise HTTPException(status_code=404, detail=f"脚本版本不存在：{code} v{revision}")
    return LogCollectScriptDetail(**row, content=content)


@router.get("/revisions", response_model=LogCollectScriptRevisionListResponse)
async def list_revisions(
    code: str = Query(min_length=1, max_length=128),
    service: LogCollectScriptService = Depends(get_service),
) -> LogCollectScriptRevisionListResponse:
    rows = await service.list_revisions(code)
    return LogCollectScriptRevisionListResponse(revisions=rows)


@router.get("/revision", response_model=LogCollectScriptRevisionDetail)
async def get_revision(
    code: str = Query(min_length=1, max_length=128),
    revision: int = Query(ge=1),
    service: LogCollectScriptService = Depends(get_service),
) -> LogCollectScriptRevisionDetail:
    row = await service.get_revision(code, revision)
    if row is None:
        raise HTTPException(status_code=404, detail=f"脚本版本不存在：{code} v{revision}")
    return LogCollectScriptRevisionDetail(**row)


@router.post("/validate", response_model=LogCollectScriptValidationResponse)
async def validate_script(payload: LogCollectScriptValidateRequest) -> LogCollectScriptValidationResponse:
    """保存前试算：纯函数，不落库、不读库，返回实际会下发的摘要。"""
    try:
        code = validate_code(payload.code)
        content = normalize_content(payload.content)
        interpreter = (payload.interpreter or infer_interpreter(code, content)).strip().lower()
        guessed = infer_interpreter(code, content)
        if interpreter != guessed:
            raise ScriptError(f"{code} 应使用 {guessed} 解释器（与文件后缀/shebang 不一致）", "LC-SCRIPT-400")
    except ScriptError as exc:
        return LogCollectScriptValidationResponse(ok=False, code=payload.code, issues=[str(exc)])
    return LogCollectScriptValidationResponse(
        ok=True,
        code=code,
        interpreter=interpreter,
        content_sha256=sha256_of(content),
        content_bytes=len(content.encode("utf-8")),
    )


@router.post("/create")
async def create_script(
    payload: LogCollectScriptCreateRequest,
    service: LogCollectScriptService = Depends(get_service),
    x_admin_user: str | None = Header(default=None, alias="X-Admin-User"),
) -> dict[str, Any]:
    try:
        result = await service.create(
            code=payload.code,
            content=payload.content,
            name=payload.name,
            interpreter=payload.interpreter,
            change_note=payload.change_note,
            source=payload.source,
            actor=x_admin_user,
            trace_id=get_current_trace_id(),
        )
    except ScriptError as exc:
        raise _http_error(exc) from exc
    await _audit(
        "log_collect.script.create",
        result["code"],
        {
            "revision": result["revision"],
            "sha256": result["content_sha256"],
            "bytes": result["content_bytes"],
        },
        x_admin_user,
    )
    _log(
        "log_collect_script_write",
        "新建收集脚本",
        code=result["code"],
        revision=result["revision"],
        sha256=result["content_sha256"],
        updated_by=x_admin_user,
    )
    return result


@router.post("/import")
async def import_script(
    payload: LogCollectScriptCreateRequest,
    service: LogCollectScriptService = Depends(get_service),
    x_admin_user: str | None = Header(default=None, alias="X-Admin-User"),
) -> dict[str, Any]:
    """导入：与新建同一套校验，只把来源记成 import（文件名取上传文件名）。"""
    return await create_script(payload.model_copy(update={"source": "import"}), service, x_admin_user)


@router.post("/save")
async def save_script(
    payload: LogCollectScriptCreateRequest,
    service: LogCollectScriptService = Depends(get_service),
    x_admin_user: str | None = Header(default=None, alias="X-Admin-User"),
) -> dict[str, Any]:
    """保存新版本。内置脚本 = 覆写（可恢复默认），平台新增脚本 = 迭代。"""
    try:
        result = await service.save(
            code=payload.code,
            content=payload.content,
            name=payload.name,
            interpreter=payload.interpreter,
            change_note=payload.change_note,
            actor=x_admin_user,
            trace_id=get_current_trace_id(),
        )
    except ScriptError as exc:
        raise _http_error(exc) from exc
    await _audit(
        "log_collect.script.save",
        result["code"],
        {
            "revision": result["revision"],
            "sha256": result["content_sha256"],
            "bytes": result["content_bytes"],
            "change_note": result.get("change_note"),
        },
        x_admin_user,
    )
    _log(
        "log_collect_script_write",
        "保存收集脚本新版本",
        code=result["code"],
        revision=result["revision"],
        sha256=result["content_sha256"],
        updated_by=x_admin_user,
    )
    return result


@router.post("/reset")
async def reset_script(
    code: str = Query(min_length=1, max_length=128),
    service: LogCollectScriptService = Depends(get_service),
    x_admin_user: str | None = Header(default=None, alias="X-Admin-User"),
) -> dict[str, Any]:
    """恢复默认：删除该 code 的全部覆写行，退回随镜像发布的快照。"""
    try:
        changed = await service.reset(code)
    except ScriptError as exc:
        raise _http_error(exc) from exc
    await _audit("log_collect.script.reset", code, {"changed": changed}, x_admin_user)
    _log("log_collect_script_write", "恢复默认收集脚本", code=code, changed=changed, updated_by=x_admin_user)
    return {"code": code, "changed": changed, "customized": False}


@router.patch("/enabled")
async def set_enabled(
    code: str = Query(min_length=1, max_length=128),
    enabled: bool = Query(),
    service: LogCollectScriptService = Depends(get_service),
    x_admin_user: str | None = Header(default=None, alias="X-Admin-User"),
) -> dict[str, Any]:
    """启停：停用后任何场景引用它都会在保存/下发两处被挡住。"""
    try:
        await service.set_enabled(code, enabled)
    except ScriptError as exc:
        raise _http_error(exc) from exc
    await _audit("log_collect.script.enabled", code, {"enabled": enabled}, x_admin_user)
    _log("log_collect_script_write", "启停收集脚本", code=code, enabled=enabled, updated_by=x_admin_user)
    return {"code": code, "enabled": enabled}


@router.get("/references", response_model=LogCollectScriptReferenceListResponse)
async def list_references(
    code: str = Query(min_length=1, max_length=128),
    service: LogCollectScriptService = Depends(get_service),
) -> LogCollectScriptReferenceListResponse:
    """反查引用：删除/停用前的拦截依据，也是"改这个脚本会影响谁"的答案。"""
    refs = await service.references(code)
    return LogCollectScriptReferenceListResponse(references=refs)


@router.delete("")
async def delete_script(
    code: str = Query(min_length=1, max_length=128),
    service: LogCollectScriptService = Depends(get_service),
    x_admin_user: str | None = Header(default=None, alias="X-Admin-User"),
) -> dict[str, Any]:
    """删除平台新增脚本；内置脚本只能恢复默认，且任何被引用的脚本都不允许删除。"""
    try:
        refs = await service.references(code)
        if refs:
            raise ScriptError(
                "脚本仍被场景引用（" + "、".join(f"{r['kind']}:{r['target']}" for r in refs) + "），请先解除引用",
                "LC-SCRIPT-409",
            )
        changed = await service.delete_script(code)
    except ScriptError as exc:
        raise _http_error(exc) from exc
    await _audit("log_collect.script.delete", code, {"changed": changed}, x_admin_user)
    _log("log_collect_script_write", "删除收集脚本", code=code, changed=changed, updated_by=x_admin_user)
    return {"code": code, "changed": changed}
