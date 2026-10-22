"""日志收集任务 API。

鉴权与 BOLA 防护与 environment 路由一致：网关签名身份 + 工单归属校验，
不存在/非归属统一 404，避免泄露资源存在性。
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from shared.database.postgres import DatabaseManager
from sqlalchemy.exc import IntegrityError

from ..repositories.log_collect_repo import LogCollectTaskRepository
from ..schemas.log_collect import (
    LogCollectTaskCreate,
    LogCollectTaskListResponse,
    LogCollectTaskPatch,
    LogCollectTaskResponse,
)
from ..schemas.log_collect_errors import build_problem
from ..security.auth import get_client_id, verify_case_ownership
from ..services.log_collect_service import (
    InvalidTransitionError,
    LogCollectService,
    TaskConflictError,
    TaskNotFoundError,
)


def _problem(code: str, detail: str) -> HTTPException:
    """把业务异常转成 RFC 7807 子集（status 由错误码目录决定）。"""
    problem = build_problem(code, detail)
    return HTTPException(status_code=int(problem["status"]), detail=problem)


router = APIRouter(prefix="/api/log-collect/tasks", tags=["log-collect"])

database_manager: DatabaseManager | None = None


def set_database_manager(db_manager: DatabaseManager) -> None:
    """设置数据库管理器（由 main.py 调用）。"""
    global database_manager
    database_manager = db_manager


async def get_log_collect_service() -> LogCollectService:
    if not database_manager:
        raise _problem("LC-PLATFORM-006", "Database not initialized")
    async for session in database_manager.get_session():
        yield LogCollectService(LogCollectTaskRepository(session))


@router.post("", response_model=LogCollectTaskResponse, status_code=201)
async def create_task(
    payload: LogCollectTaskCreate,
    client_id: str = Depends(get_client_id),
    service: LogCollectService = Depends(get_log_collect_service),
):
    """创建日志收集任务（同一工单只允许一个活跃任务）。"""
    await verify_case_ownership(payload.case_id, client_id, service.repository.session)
    try:
        return await service.create_task(payload, client_id)
    except TaskConflictError as exc:
        raise _problem(exc.code, str(exc)) from exc
    except IntegrityError as exc:
        # 并发窗口内两个请求同时通过应用层检查：数据库部分唯一索引兜底。
        raise _problem("LC-PLUGIN-007", "该工单已有进行中的日志收集任务") from exc


@router.get("", response_model=LogCollectTaskListResponse)
async def list_tasks(
    case_id: str = Query(..., min_length=1, max_length=20, description="工单号"),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    client_id: str = Depends(get_client_id),
    service: LogCollectService = Depends(get_log_collect_service),
):
    """按工单查询任务列表（倒序）。"""
    await verify_case_ownership(case_id, client_id, service.repository.session)
    items, total = await service.list_tasks(case_id, limit=limit, offset=offset)
    return {"total": total, "items": items}


@router.get("/{task_id}", response_model=LogCollectTaskResponse)
async def get_task(
    task_id: UUID,
    client_id: str = Depends(get_client_id),
    service: LogCollectService = Depends(get_log_collect_service),
):
    """查询单个任务。"""
    try:
        task = await service.get_task(task_id)
    except TaskNotFoundError as exc:
        raise _problem(TaskNotFoundError.code, "Task not found") from exc
    await verify_case_ownership(task.case_id, client_id, service.repository.session)
    return task


@router.patch("/{task_id}", response_model=LogCollectTaskResponse)
async def update_task(
    task_id: UUID,
    patch: LogCollectTaskPatch,
    client_id: str = Depends(get_client_id),
    service: LogCollectService = Depends(get_log_collect_service),
):
    """回写状态/进度/制品元数据（由前端从 bridge 轮询结果上报）。"""
    try:
        task = await service.get_task(task_id)
    except TaskNotFoundError as exc:
        raise _problem(TaskNotFoundError.code, "Task not found") from exc
    await verify_case_ownership(task.case_id, client_id, service.repository.session)
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
    try:
        task = await service.get_task(task_id)
    except TaskNotFoundError as exc:
        raise _problem(TaskNotFoundError.code, "Task not found") from exc
    await verify_case_ownership(task.case_id, client_id, service.repository.session)
    try:
        return await service.cancel_task(task_id, reason)
    except TaskConflictError as exc:
        raise _problem(exc.code, str(exc)) from exc
