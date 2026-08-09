"""KBD 任务范围、阶段选择和执行模式。

KBD CLI 是任务管理器，不再把 Fetch/Import 的实现细节暴露成不同的覆盖参数。
每个 ``support_id + stage`` 是一个任务，命令只选择任务，不改变阶段内部的业务契约。

执行模式严格互斥：

* 默认：未执行 + 失败；
* ``--resume``：未执行；
* ``--failed``：失败；
* ``--rework``：全部任务（可按 KBD 生命周期状态过滤）。

本模块只负责纯规划和状态选择；实际 Stage 的执行仍由 ``pipeline.py`` 负责。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from .pipeline import Stage, resolve_stages


class TaskMode(StrEnum):
    """一次任务运行的唯一选择模式。"""

    DEFAULT = "default"
    RESUME = "resume"
    FAILED = "failed"
    REWORK = "rework"


ALL_STAGE_NAMES = tuple(stage.name.lower().replace("_", "-") for stage in Stage)
# KBD 生命周期状态（与 kb-service KbdEntry.status 契约一致）。
REWORK_STATUS_NAMES = ("draft", "published", "rejected", "archived")
REWORK_STATUS_SET = frozenset(REWORK_STATUS_NAMES)


@dataclass(frozen=True)
class TaskState:
    """一个 support_id + stage 的当前状态。

    ``executed``/``success`` 是任务状态；``rework`` 是上一次执行留下的重做标记。
    ``item_states`` 用于 Vision 等包含多个子任务的 Stage。
    """

    executed: bool = False
    success: bool | None = None
    rework: bool = False
    item_states: dict[str, TaskState] | None = None

    @property
    def failed(self) -> bool:
        return self.executed and self.success is False

    @property
    def unfinished(self) -> bool:
        return not self.executed


def parse_task_mode(*, resume: bool, failed: bool, rework: bool) -> TaskMode:
    """把 CLI 三个互斥开关解析为唯一模式。"""

    selected = sum((resume, failed, rework))
    if selected > 1:
        raise ValueError("--resume、--failed、--rework 三者只能选择一个")
    if resume:
        return TaskMode.RESUME
    if failed:
        return TaskMode.FAILED
    if rework:
        return TaskMode.REWORK
    return TaskMode.DEFAULT


def parse_rework_statuses(value: str | None) -> tuple[str, ...]:
    """解析 ``--rework`` 的可选 KBD 状态范围。"""

    if value is None or not value.strip():
        return ("draft",)
    statuses = tuple(dict.fromkeys(item.strip().lower() for item in value.split(",") if item.strip()))
    invalid = sorted(set(statuses) - REWORK_STATUS_SET)
    if invalid:
        raise ValueError(
            f"--rework 状态非法: {', '.join(invalid)}；合法值：{','.join(REWORK_STATUS_NAMES)}"
        )
    return statuses


def parse_stage_names(value: str | None) -> tuple[Stage, ...]:
    """解析阶段名称，默认返回全部阶段并按 DAG 拓扑序排列。"""

    if value is None or value.strip().lower() in {"", "all"}:
        return tuple(Stage)

    aliases = {
        stage.name.lower().replace("_", "-"): stage
        for stage in Stage
    }
    aliases.update({str(int(stage)): stage for stage in Stage})
    requested: list[Stage] = []
    for raw_name in value.split(","):
        name = raw_name.strip().lower()
        if name not in aliases:
            allowed = ",".join((*ALL_STAGE_NAMES, "all"))
            raise ValueError(f"未知 Stage: {raw_name.strip()}；合法值：{allowed}")
        if aliases[name] not in requested:
            requested.append(aliases[name])
    return tuple(resolve_stages(requested))


def task_is_selected(state: TaskState, mode: TaskMode) -> bool:
    """判断一个任务是否进入当前执行计划。"""

    if mode is TaskMode.DEFAULT:
        return state.unfinished or state.failed
    if mode is TaskMode.RESUME:
        return state.unfinished
    if mode is TaskMode.FAILED:
        return state.failed
    if mode is TaskMode.REWORK:
        return True
    raise AssertionError(f"unknown task mode: {mode}")


def select_task_ids(
    task_ids: Iterable[str],
    *,
    stage: Stage,
    states: dict[tuple[str, Stage], TaskState],
    mode: TaskMode,
) -> list[str]:
    """按模式选择某个 Stage 的任务 ID，并保持输入顺序、去除重复值。"""

    selected: list[str] = []
    seen: set[str] = set()
    for support_id in task_ids:
        support_id = str(support_id).strip()
        if not support_id or support_id in seen:
            continue
        seen.add(support_id)
        if task_is_selected(states.get((support_id, stage), TaskState()), mode):
            selected.append(support_id)
    return selected


def stage_cli_name(stage: Stage) -> str:
    return stage.name.lower().replace("_", "-")
