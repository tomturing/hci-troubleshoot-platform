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
    # 纠正（correct）：平台已归档，但完成判定不可信（典型是同 ticket 的历史任务报告）。
    # 只在前端判定"完成态不可信"时，由用户显式点击触发，且会清空该任务的产物记录。
    "completed": frozenset({"collecting"}),
    # 接管（adopt）：平台已判失败/取消，但节点上任务仍在跑。
    # 节点是唯一真相源，允许把投影改回收集中，继续跟进；不做自动重试，避免重复采集。
    "failed": frozenset({"collecting"}),
    "cancelled": frozenset({"collecting"}),
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
            # 下发锚定的报告指纹必须随任务落库：前端内存里的基线在关面板、
            # 换浏览器、重启后会丢失，届时就无法判断"节点报告属于哪一次下发"。
            detail=(
                {"baseline_report_hash": payload.baseline_report_hash}
                if payload.baseline_report_hash
                else {}
            ),
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

        # 必须先把旧状态存下来：下面的 setattr 会把 task.status 直接改成目标值，
        # 之后再用 task.status 判断"是不是从终态复活"永远为假，接管/纠正分支会变成死代码。
        previous_status = task.status

        for key, value in data.items():
            setattr(task, key, value)

        if target_status in TERMINAL_STATUSES:
            task.finished_at = datetime.now(UTC)
            if target_status == "completed":
                # 完成即 100%：插件 final 阶段偶发未刷新 progress，平台侧做确定性收敛。
                task.progress = 100
        elif target_status == "collecting" and previous_status in TERMINAL_STATUSES:
            # 接管 / 纠正：终态是"平台投影"，节点仍在跑时可以被覆盖。
            if previous_status == "completed":
                # 原完成记录里的产物未必属于本次任务（同 ticket 重复 create 会拿到上一次的报告），
                # 继续挂着会变成"采集中却显示产物位置"的新误导。清空产物字段，原值留审计；
                # 注意：只改 DB 投影，节点与日志服务器上的压缩包一律不删。
                audit = (
                    f"原完成记录已按节点态纠正：archive_path={task.archive_path}, "
                    f"archive_size={task.archive_size}, archive_sha256={task.archive_sha256}, "
                    f"relay_status={task.relay_status}"
                )
                task.archive_path = None
                task.archive_size = None
                task.archive_sha256 = None
                task.relay_status = "none"
                task.relay_chunk_index = None
                task.relay_chunk_total = None
                task.relay_restarts = 0
                task.cleanup_state = "none"
                task.cleanup_reason = None
                task.error_code = "corrected_by_node"
                task.error_message = audit
            else:
                # 接管失败/取消的任务：清掉上次的失败痕迹，
                # 否则界面会一边显示"采集中"一边显示旧错误原因。
                task.error_code = None
                task.error_message = None
            task.finished_at = None
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
