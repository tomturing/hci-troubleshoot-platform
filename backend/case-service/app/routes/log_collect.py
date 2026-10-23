"""日志收集任务 API。

鉴权与 BOLA 防护与 environment 路由一致：网关签名身份 + 工单归属校验，
不存在/非归属统一 404，避免泄露资源存在性。
"""

from __future__ import annotations

import asyncio
import logging
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from shared.database.postgres import DatabaseManager
from sqlalchemy.exc import IntegrityError

from ..config import settings
from ..repositories.log_collect_repo import LogCollectTaskRepository
from ..schemas.log_collect import (
    LogCollectTaskCreate,
    LogCollectTaskListResponse,
    LogCollectTaskPatch,
    LogCollectTaskResponse,
    UploadCompleteRequest,
    UploadCompleteResponse,
    UploadInitRequest,
    UploadInitResponse,
    UploadPartResponse,
    UploadStatusResponse,
)
from ..schemas.log_collect_errors import build_problem
from ..security.auth import get_client_id, verify_case_exists
from ..services.log_collect_relay_service import LogCollectRelayService, SangforRelayClient
from ..services.log_collect_service import (
    InvalidTransitionError,
    LogCollectService,
    TaskConflictError,
    TaskNotFoundError,
)
from ..services.log_collect_storage import LocalLogCollectStorage
from ..services.log_collect_upload_service import LogCollectUploadError, LogCollectUploadService

logger = logging.getLogger(__name__)


def _problem(code: str, detail: str, **extra) -> HTTPException:
    """把业务异常转成 RFC 7807 子集（status 由错误码目录决定）。

    ``extra`` 用于 409 这类需要携带结构化上下文（active_task）的场景。
    """
    problem = build_problem(code, detail)
    problem.update(extra)
    return HTTPException(status_code=int(problem["status"]), detail=problem)


def _active_task_summary(task, client_id: str) -> dict | None:
    """409 附带的活跃任务摘要（跨客户端脱敏）。

    只暴露"有任务、进行到哪、是不是本人"，不暴露 origin_node_ip / ips /
    archive_path：这些是采集目标与制品位置，跨客户端可见会构成信息泄露。
    """
    if task is None:
        return None
    return {
        "task_id": str(task.task_id),
        "status": task.status,
        "progress": task.progress,
        "created_by_me": task.created_by == client_id,
        "created_at": task.created_at.isoformat() if task.created_at else None,
    }


router = APIRouter(prefix="/api/log-collect/tasks", tags=["log-collect"])

database_manager: DatabaseManager | None = None
upload_storage: LocalLogCollectStorage | None = None


def set_database_manager(db_manager: DatabaseManager) -> None:
    """设置数据库管理器（由 main.py 调用）。"""
    global database_manager
    database_manager = db_manager


def set_upload_storage(storage: LocalLogCollectStorage) -> None:
    """注入分块存储（测试可替换为临时目录）。"""
    global upload_storage
    upload_storage = storage


def get_upload_storage() -> LocalLogCollectStorage:
    global upload_storage
    if upload_storage is None:
        upload_storage = LocalLogCollectStorage(settings.LOG_COLLECT_STORAGE_ROOT)
    return upload_storage


async def get_log_collect_service() -> LogCollectService:
    if not database_manager:
        raise _problem("LC-PLATFORM-006", "Database not initialized")
    async for session in database_manager.get_session():
        yield LogCollectService(LogCollectTaskRepository(session))


async def get_upload_service(
    service: LogCollectService = Depends(get_log_collect_service),
) -> LogCollectUploadService:
    return LogCollectUploadService(service.repository, get_upload_storage())


def _upload_problem(exc: LogCollectUploadError) -> HTTPException:
    """上传类异常沿用 RFC 7807 结构，但以异常自身的 HTTP 状态为准。"""
    problem = build_problem(exc.code, str(exc))
    problem["status"] = exc.status
    return HTTPException(status_code=exc.status, detail=problem)


async def get_relay_service() -> LogCollectRelayService:
    """代传服务依赖：长任务自行开 session，因此注入 session 工厂而不是单个 session。"""
    if not database_manager:
        raise _problem("LC-PLATFORM-006", "Database not initialized")
    if not settings.LOG_COLLECT_RELAY_ENABLED:
        raise _problem("LC-TRANSFER-003", "日志服务器代传功能已禁用")
    return LogCollectRelayService(
        database_manager.async_session_factory,
        get_upload_storage(),
        SangforRelayClient(settings.LOG_COLLECT_RELAY_URL),
    )


_relay_tasks: set[asyncio.Task] = set()


def _on_relay_done(task: asyncio.Task) -> None:
    """后台任务必须取走异常，否则 Python 只打印 'never retrieved' 且无业务上下文。"""
    _relay_tasks.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("日志包代传失败 task=%s error=%s", task.get_name(), exc)


@router.post("", response_model=LogCollectTaskResponse, status_code=201)
async def create_task(
    payload: LogCollectTaskCreate,
    client_id: str = Depends(get_client_id),
    service: LogCollectService = Depends(get_log_collect_service),
):
    """创建日志收集任务（同一工单只允许一个活跃任务）。

    只校验工单存在：允许跨客户端复用同一工单号（方案 A），任务按 created_by 隔离。
    """
    await verify_case_exists(payload.case_id, service.repository.session)
    try:
        return await service.create_task(payload, client_id)
    except TaskConflictError as exc:
        raise _problem(
            exc.code,
            str(exc),
            active_task=_active_task_summary(exc.active_task, client_id),
        ) from exc
    except IntegrityError as exc:
        # 并发窗口内两个请求同时通过应用层检查：数据库部分唯一索引兜底。
        # 这里不查活跃任务（session 已失效），前端可用"查看任务状态"重新拉取。
        raise _problem("LC-PLUGIN-007", "该工单已有进行中的日志收集任务") from exc


@router.get("", response_model=LogCollectTaskListResponse)
async def list_tasks(
    case_id: str = Query(..., min_length=1, max_length=20, description="工单号"),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    client_id: str = Depends(get_client_id),
    service: LogCollectService = Depends(get_log_collect_service),
):
    """按工单查询任务列表（倒序），只返回当前身份创建的任务。"""
    await verify_case_exists(case_id, service.repository.session)
    items, total = await service.list_tasks(case_id, created_by=client_id, limit=limit, offset=offset)
    return {"total": total, "items": items}


@router.get("/{task_id}", response_model=LogCollectTaskResponse)
async def get_task(
    task_id: UUID,
    client_id: str = Depends(get_client_id),
    service: LogCollectService = Depends(get_log_collect_service),
):
    """查询单个任务（仅任务创建者可见）。"""
    return await _owned_task(task_id, client_id, service)


@router.patch("/{task_id}", response_model=LogCollectTaskResponse)
async def update_task(
    task_id: UUID,
    patch: LogCollectTaskPatch,
    client_id: str = Depends(get_client_id),
    service: LogCollectService = Depends(get_log_collect_service),
):
    """回写状态/进度/制品元数据（由前端从 bridge 轮询结果上报）。"""
    await _owned_task(task_id, client_id, service)
    try:
        return await service.update_task(task_id, patch)
    except (InvalidTransitionError, TaskConflictError) as exc:
        raise _problem(exc.code, str(exc)) from exc


@router.post("/{task_id}/cancel", response_model=LogCollectTaskResponse)
async def cancel_task(
    task_id: UUID,
    reason: str | None = Query(None, max_length=200),
    client_id: str = Depends(get_client_id),
    service: LogCollectService = Depends(get_log_collect_service),
):
    """标记任务取消。

    注意：本接口只改平台状态；真正终止采集必须由前端经 bridge 执行
    ``acli log_collect cancel``（会 kill 集群采集进程并删除已收集的包）。
    """
    await _owned_task(task_id, client_id, service)
    try:
        return await service.cancel_task(task_id, reason)
    except TaskConflictError as exc:
        raise _problem(exc.code, str(exc)) from exc


# ── P3 段②：分块上传（控制面，经 API Gateway）────────────────────────────────


async def _owned_task(task_id: UUID, client_id: str, service: LogCollectService):
    """任务按创建者鉴权：跨客户端复用同一工单号时，彼此不可见/不可操作。

    与工单归属解耦后，同一 case_id 下可以同时存在多个客户端各自的任务；
    非创建者统一 404，避免泄露任务存在性。
    """
    try:
        task = await service.get_task(task_id)
    except TaskNotFoundError as exc:
        raise _problem(TaskNotFoundError.code, "Task not found") from exc
    if task.created_by != client_id:
        raise _problem(TaskNotFoundError.code, "Task not found")
    return task


@router.post("/{task_id}/uploads", response_model=UploadInitResponse)
async def init_upload(
    task_id: UUID,
    payload: UploadInitRequest,
    client_id: str = Depends(get_client_id),
    service: LogCollectService = Depends(get_log_collect_service),
    upload_service: LogCollectUploadService = Depends(get_upload_service),
):
    """初始化上传会话。

    幂等：同一任务重复调用返回既有会话与已传分片，客户端据此跳过已传部分；
    若声明的总大小与既有会话不一致则 409，要求显式 abort 后重建，避免续传出错包。
    """
    task = await _owned_task(task_id, client_id, service)
    try:
        return await upload_service.init(
            task,
            total_size=payload.total_size,
            expected_sha256=payload.expected_sha256,
        )
    except LogCollectUploadError as exc:
        raise _upload_problem(exc) from exc


@router.get("/{task_id}/uploads", response_model=UploadStatusResponse)
async def get_upload_status(
    task_id: UUID,
    client_id: str = Depends(get_client_id),
    service: LogCollectService = Depends(get_log_collect_service),
    upload_service: LogCollectUploadService = Depends(get_upload_service),
):
    """查询已上传分片，供断点续传使用。"""
    await _owned_task(task_id, client_id, service)
    try:
        return await upload_service.status(task_id)
    except LogCollectUploadError as exc:
        raise _upload_problem(exc) from exc


@router.post("/{task_id}/uploads/complete", response_model=UploadCompleteResponse)
async def complete_upload(
    task_id: UUID,
    payload: UploadCompleteRequest,
    client_id: str = Depends(get_client_id),
    service: LogCollectService = Depends(get_log_collect_service),
    upload_service: LogCollectUploadService = Depends(get_upload_service),
):
    """流式合并分片并落库制品元数据。

    只更新 ``archive_path/archive_size/archive_sha256``，不改变任务状态：
    状态跃迁由前端在 P3 各阶段显式 PATCH，避免与状态机互相覆盖。
    """
    task = await _owned_task(task_id, client_id, service)
    try:
        return await upload_service.complete(
            task,
            parts=payload.parts,
            declared_sha256=payload.sha256,
            declared_size=payload.size,
        )
    except LogCollectUploadError as exc:
        raise _upload_problem(exc) from exc


@router.post("/{task_id}/uploads/abort", status_code=204)
async def abort_upload(
    task_id: UUID,
    client_id: str = Depends(get_client_id),
    service: LogCollectService = Depends(get_log_collect_service),
    upload_service: LogCollectUploadService = Depends(get_upload_service),
) -> None:
    """中止上传并清理临时分片（不触碰已合并的成品）。"""
    await _owned_task(task_id, client_id, service)
    await upload_service.abort(task_id)


# ── P3 段③：代传 5888 日志服务器（异步长任务）─────────────────────────────────


@router.post("/{task_id}/relay", status_code=202)
async def start_relay(
    task_id: UUID,
    force: bool = Query(False, description="仅当上一进程崩溃导致 relay_status 卡在 running 时使用"),
    client_id: str = Depends(get_client_id),
    service: LogCollectService = Depends(get_log_collect_service),
    relay_service: LogCollectRelayService = Depends(get_relay_service),
):
    """启动代传。

    幂等：只有把 ``relay_status`` 从非 running 改成 running 的调用者才会真正执行，
    因此重复点击或前端重试不会产生两个并发上传（20 GiB 重传代价过高）。
    """
    task = await _owned_task(task_id, client_id, service)
    if not task.archive_path:
        raise _problem("LC-TRANSFER-001", "平台尚无制品，无法代传")

    started = await relay_service.trigger(task_id, force=force)
    if started:
        background = asyncio.create_task(relay_service.run(task_id))
        _relay_tasks.add(background)
        background.add_done_callback(_on_relay_done)
    return {
        "task_id": str(task_id),
        "started": started,
        "relay_status": "running" if started else task.relay_status,
    }


@router.get("/{task_id}/relay")
async def get_relay_status(
    task_id: UUID,
    client_id: str = Depends(get_client_id),
    service: LogCollectService = Depends(get_log_collect_service),
    relay_service: LogCollectRelayService = Depends(get_relay_service),
):
    """查询代传状态与断点位置（供前端展示与人工重试判断）。"""
    await _owned_task(task_id, client_id, service)
    snapshot = await relay_service.snapshot(task_id)
    if snapshot is None:
        raise _problem(TaskNotFoundError.code, "Task not found")
    return snapshot


# ── 对象数据面：直连 case-service，绕过 API Gateway 的体积限制 ────────────────

upload_router = APIRouter(prefix="/api/direct/log-collect-uploads", tags=["log-collect-upload"])


@upload_router.put("/{task_id}/parts/{part_number}", response_model=UploadPartResponse)
async def upload_part_direct(
    task_id: UUID,
    part_number: int,
    request: Request,
    upload_token: str = Header(alias="X-Upload-Token", min_length=8, max_length=256),
    part_sha256: str | None = Header(default=None, alias="X-Part-SHA256", pattern=r"^[0-9a-f]{64}$"),
    upload_service: LogCollectUploadService = Depends(get_upload_service),
):
    """流式写入单个分片。

    该路径**不得**配置到 API Gateway：网关默认 1 MiB、豁免 10 MiB，
    而分片为 32 MiB。鉴权改用 init 签发的短期 token（绑定 task_id）。
    """
    if not upload_service.verify_token(str(task_id), upload_token):
        raise HTTPException(
            status_code=401,
            detail=build_problem("LC-TRANSFER-004", "上传 token 无效或已过期"),
        )
    try:
        return await upload_service.record_part(
            task_id,
            part_number=part_number,
            chunks=request.stream(),
            claimed_sha256=part_sha256,
        )
    except LogCollectUploadError as exc:
        raise _upload_problem(exc) from exc
