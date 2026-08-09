"""KBD 任务状态账本。

Progress 文件记录一次运行的观测；本账本记录跨运行的 ``support_id + stage`` 当前状态，
因此 ``--resume``、``--failed`` 和 ``--rework`` 不再依赖某次日志文件的偶然存在。
写入采用同目录临时文件替换，避免进程中断留下半个 JSON。
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .config import settings
from .pipeline import Stage
from .task_manager import TaskState, stage_cli_name


def state_path() -> Path:
    return settings.KBD_LOGS_DIR / "task-state.json"


def manifest_path(execution_id: str) -> Path:
    """一次执行的不可变任务清单位置。"""

    if not execution_id or any(char not in "0123456789_" for char in execution_id):
        raise ValueError(f"execution_id 非法: {execution_id!r}")
    return settings.KBD_LOGS_DIR / "task-manifests" / f"{execution_id}.json"


def save_execution_manifest(manifest: dict[str, Any]) -> None:
    """写入不可变 manifest；同一 execution_id 不允许被另一份计划覆盖。"""

    execution_id = str(manifest.get("execution_id") or "")
    path = manifest_path(execution_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if path.exists() and path.read_text(encoding="utf-8") != encoded:
        raise ValueError(f"execution_id 已有不同的任务 manifest: {execution_id}")
    if not path.exists():
        path.write_text(encoded, encoding="utf-8")


def load_execution_manifest(execution_id: str) -> dict[str, Any] | None:
    """读取历史任务范围；不再回退到旧 progress 文件。"""

    path = manifest_path(execution_id)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"任务 manifest 无法读取: {execution_id}") from exc
    if not isinstance(payload, dict) or payload.get("execution_id") != execution_id:
        raise ValueError(f"任务 manifest 内容非法: {execution_id}")
    return payload


def _state_from_json(raw: Any) -> TaskState:
    if not isinstance(raw, dict):
        return TaskState()
    item_states = raw.get("item_states")
    parsed_items = None
    if isinstance(item_states, dict):
        parsed_items = {str(key): _state_from_json(value) for key, value in item_states.items()}
    success = raw.get("success")
    if success not in {True, False, None}:
        success = None
    return TaskState(
        executed=bool(raw.get("executed", False)),
        success=success,
        rework=bool(raw.get("rework", False)),
        item_states=parsed_items,
    )


def load_state(path: Path | None = None) -> dict[tuple[str, Stage], TaskState]:
    path = path or state_path()
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    tasks = raw.get("tasks", {}) if isinstance(raw, dict) else {}
    result: dict[tuple[str, Stage], TaskState] = {}
    if not isinstance(tasks, dict):
        return result
    for support_id, stage_map in tasks.items():
        if not isinstance(stage_map, dict):
            continue
        for stage_name, value in stage_map.items():
            try:
                stage = Stage[stage_name.upper().replace("-", "_")]
            except KeyError:
                continue
            result[(str(support_id), stage)] = _state_from_json(value)
    return result


def save_state(states: dict[tuple[str, Stage], TaskState], path: Path | None = None) -> None:
    path = path or state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tasks: dict[str, dict[str, Any]] = {}
    for (support_id, stage), state in sorted(states.items()):
        tasks.setdefault(support_id, {})[stage_cli_name(stage)] = asdict(state)
    payload = {"version": 1, "tasks": tasks}
    fd, temporary = tempfile.mkstemp(prefix="task-state.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def merge_run_progress(
    progress: dict[str, Any],
    states: dict[tuple[str, Stage], TaskState],
    stage_stats: dict[str, Any] | None = None,
) -> dict[tuple[str, Stage], TaskState]:
    """将一次 run 的 progress 合并进跨运行任务账本。"""

    kbds = progress.get("kbds", {}) if isinstance(progress, dict) else {}
    if not isinstance(kbds, dict):
        return states
    for support_id, stage_map in kbds.items():
        if not isinstance(stage_map, dict):
            continue
        for stage_name, status in stage_map.items():
            try:
                stage = Stage[stage_name.upper().replace("-", "_")]
            except KeyError:
                continue
            if status in {"done", "success"}:
                states[(str(support_id), stage)] = TaskState(executed=True, success=True)
            elif status == "failed":
                states[(str(support_id), stage)] = TaskState(
                    executed=True,
                    success=False,
                    rework=True,
                )
            elif status == "blocked_by_dependency":
                # 阻断表示本阶段没有执行，不应伪装成“失败任务”；默认/resume
                # 下次仍会重新评估依赖，--failed 则只处理真正调用失败的任务。
                states[(str(support_id), stage)] = TaskState()
    vision_items = ((stage_stats or {}).get("vision") or {}).get("item_states", {})
    if isinstance(vision_items, dict):
        for support_id, item_states in vision_items.items():
            key = (str(support_id), Stage.VISION)
            previous = states.get(key, TaskState())
            if not isinstance(item_states, dict):
                continue
            parsed = {
                str(item_id): _state_from_json(raw)
                for item_id, raw in item_states.items()
            }
            states[key] = TaskState(
                executed=previous.executed,
                success=previous.success,
                rework=previous.rework,
                item_states=parsed,
            )
    return states
