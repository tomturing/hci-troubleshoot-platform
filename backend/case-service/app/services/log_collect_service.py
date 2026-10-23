"""日志收集任务服务：状态机与业务规则。

状态机（与 acli 阶段一一对应）：

    pending → probing → installing → collecting → packaging → uploading → completed
       └───────────────┴──────────────┴────────────┴───────────┴──→ failed / cancelled

允许跳步（例如未安装 acli 时 probing → collecting、无上传时 packaging → completed），
但禁止从终态复活、禁止进度回退——回退通常意味着前端轮询发生了节点错配（progress
只反映发起节点，路由错了会读到旧任务或空任务）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from ..models.log_collect import LogCollectTask
from ..repositories.log_collect_repo import LogCollectTaskRepository
from ..schemas.log_collect import (
    TERMINAL_STATUSES,
    LogCollectTaskCreate,
    LogCollectTaskPatch,
)

_ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset({"probing", "installing", "collecting", "failed", "cancelled"}),
    "probing": frozenset({"installing", "collecting", "failed", "cancelled"}),
    "installing": frozenset({"collecting", "failed", "cancelled"}),
    "collecting": frozenset({"packaging", "uploading", "completed", "failed", "cancelled"}),
    "packaging": frozenset({"uploading", "completed", "failed", "cancelled"}),
    "uploading": frozenset({"completed", "failed", "cancelled"}),
    "completed": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
}


class LogCollectError(Exception):
    """带规范错误码的业务异常基类（错误码见 log_collect_error_catalog.json）。"""

    code = "LC-PLATFORM-006"

    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        if code:
            self.code = code


class TaskNotFoundError(LogCollectError):
    """任务不存在（或不属于当前工单）。"""

    code = "LC-PLUGIN-008"


class TaskConflictError(LogCollectError):
    """同一工单已有活跃任务，或任务已结束无法操作。

    ``active_task`` 供路由层生成 409 摘要（状态/进度/是否本人），
    让前端直接展示"查看任务状态"而不必再查一次。
    """

    code = "LC-PLUGIN-007"

    def __init__(
        self,
        message: str,
        active_task: LogCollectTask | None = None,
        code: str | None = None,
    ):
        super().__init__(message, code=code)
        self.active_task = active_task


class InvalidTransitionError(LogCollectError):
    """非法状态转换或进度回退。"""

    code = "LC-PLATFORM-003"


class LogCollectService:
    def __init__(self, repo: LogCollectTaskRepository):
        self.repository = repo

    @staticmethod
    def validate_transition(current: str, target: str) -> None:
        if current == target:
            return
        if target not in _ALLOWED_TRANSITIONS.get(current, frozenset()):
            raise InvalidTransitionError(f"非法状态转换：{current} → {target}")

    async def create_task(self, payload: LogCollectTaskCreate, client_id: str) -> LogCollectTask:
        # 应用层先判一次（友好报错），数据库部分唯一索引兜底并发。
        active = await self.repository.get_active_by_case(payload.case_id)
        if active is not None:
            raise TaskConflictError(
                f"工单 {payload.case_id} 已有进行中的日志收集任务：{active.task_id}",
                active_task=active,
            )

        task = LogCollectTask(
            # 工单号只有一个：acli -s、产物名与任务归属全部使用 case_id
            case_id=payload.case_id,
            origin_node_ip=payload.origin_node_ip,
            modules=list(payload.modules),
            date_from=payload.date_from,
            date_to=payload.date_to,
            ips=list(payload.ips),
            extra_paths=list(payload.extra_paths) or None,
            remote_url=payload.remote_url,
            status="pending",
            progress=0,
            detail={},
            created_by=client_id,
            # trace_id 由平台兜底生成：前端若未传，任务仍必须有一个可贯穿
            # 「平台 → bridge → HCI → 回采日志」的链路 ID，否则远端无法按链路定位。
            trace_id=payload.trace_id or uuid4().hex,
        )
        return await self.repository.create(task)

    async def get_task(self, task_id) -> LogCollectTask:
        task = await self.repository.get_by_id(task_id)
        if task is None:
            raise TaskNotFoundError(str(task_id))
        return task

    async def list_tasks(
        self,
        case_id: str,
        *,
        created_by: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ):
        return await self.repository.list_by_case(case_id, created_by=created_by, limit=limit, offset=offset)

    async def update_task(self, task_id, patch: LogCollectTaskPatch) -> LogCollectTask:
        task = await self.get_task(task_id)
        data = patch.model_dump(exclude_none=True)
        target_status = data.get("status")

        if target_status is not None:
            self.validate_transition(task.status, target_status)

        new_progress = data.get("progress")
        if new_progress is not None and task.status not in TERMINAL_STATUSES and new_progress < task.progress:
            raise InvalidTransitionError(f"进度不能回退：{task.progress} → {new_progress}", code="LC-PLATFORM-004")

        for key, value in data.items():
            setattr(task, key, value)

        if target_status in TERMINAL_STATUSES:
            task.finished_at = datetime.now(UTC)
            if target_status == "completed":
                # 完成即 100%：插件 final 阶段偶发未刷新 progress，平台侧做确定性收敛。
                task.progress = 100
        task.updated_at = datetime.now(UTC)

        await self.repository.flush()
        await self.repository.refresh(task)
        return task

    async def cancel_task(self, task_id, reason: str | None = None) -> LogCollectTask:
        task = await self.get_task(task_id)
        if task.status in TERMINAL_STATUSES:
            raise TaskConflictError(f"任务已结束（{task.status}），无法取消", code="LC-PLATFORM-003")
        task.status = "cancelled"
        task.error_code = "cancelled_by_user"
        task.error_message = reason or "用户取消"
        task.finished_at = datetime.now(UTC)
        task.updated_at = datetime.now(UTC)
        await self.repository.flush()
        await self.repository.refresh(task)
        return task
