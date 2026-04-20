"""
scripts/kbd/progress.py — KBD Pipeline 进度追踪模块

功能：
  - 生成 run_id（YYYYMMDD_HHMMSS 格式）
  - 保存进度到 logs/progress_{run_id}.json
  - 跟踪各 stage 的完成状态
  - 支持读取已有进度文件实现 resume

进度 JSON 结构：
{
  "run_id": "20260420_100000",
  "started_at": "2026-04-20T10:00:00",
  "finished_at": null,
  "total_ids": 100,
  "stages_run": ["fetch", "vision"],
  "stages": {
    "fetch": {"completed_ids": [], "failed_ids": [], "skipped_ids": []},
    "vision": {"completed_ids": [], "failed_ids": [], "skipped_ids": []}
  },
  "cases": {
    "15414": {"fetch": "done", "vision": "pending", "import": "pending", "classify": "pending"}
  }
}
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import settings

logger = logging.getLogger("kbd.progress")

# 所有 stage 名称
ALL_STAGES = ["fetch", "vision", "import", "classify"]


def generate_run_id() -> str:
    """生成 run_id（YYYYMMDD_HHMMSS 格式）"""
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def get_progress_path(run_id: str) -> Path:
    """获取进度文件路径"""
    return settings.KBD_LOGS_DIR / f"progress_{run_id}.json"


def get_log_path(run_id: str) -> Path:
    """获取日志文件路径"""
    return settings.KBD_LOGS_DIR / f"kbd_{run_id}.log"


def init_progress(run_id: str, case_ids: list[str], stages: list[str]) -> dict[str, Any]:
    """
    初始化进度结构并保存到文件。

    Args:
        run_id: 运行标识
        case_ids: 要处理的案例 ID 列表
        stages: 要运行的 stage 列表

    Returns:
        进度字典
    """
    progress: dict[str, Any] = {
        "run_id": run_id,
        "started_at": datetime.now().isoformat(),
        "finished_at": None,
        "total_ids": len(case_ids),
        "stages_run": stages,
        "stages": {
            stage: {"completed_ids": [], "failed_ids": [], "skipped_ids": []}
            for stage in ALL_STAGES
        },
        "cases": {
            cid: {stage: "pending" for stage in ALL_STAGES}
            for cid in case_ids
        },
    }
    save_progress(run_id, progress)
    logger.info("进度初始化完成 run_id=%s cases=%d stages=%s", run_id, len(case_ids), stages)
    return progress


def save_progress(run_id: str, progress: dict[str, Any]) -> None:
    """保存进度到 JSON 文件"""
    settings.KBD_LOGS_DIR.mkdir(parents=True, exist_ok=True)
    path = get_progress_path(run_id)
    path.write_text(json.dumps(progress, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.debug("进度已保存 path=%s", path)


def load_progress(run_id: str) -> dict[str, Any] | None:
    """加载已有进度文件，失败返回 None"""
    path = get_progress_path(run_id)
    if not path.exists():
        logger.debug("进度文件不存在 run_id=%s", run_id)
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        logger.info("进度文件加载成功 run_id=%s cases=%d", run_id, len(data.get("cases", {})))
        return data
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("进度文件损坏 path=%s 原因=%s", path, exc)
        return None


def update_stage_status(
    progress: dict[str, Any],
    stage: str,
    support_id: str,
    status: str,  # "done" | "failed" | "skipped" | "pending"
) -> None:
    """
    更新单个案例在特定 stage 的状态。

    同时更新 cases 字段和 stages 统计字段。
    """
    # 更新 cases 字段
    if support_id in progress.get("cases", {}):
        progress["cases"][support_id][stage] = status

    # 更新 stages 统计
    stages_stats = progress.get("stages", {}).get(stage, {})
    stats_key = f"{status}_ids"
    if stats_key in stages_stats:
        id_list = stages_stats[stats_key]
        if support_id not in id_list:
            id_list.append(support_id)

    logger.debug("状态更新 case=%s stage=%s status=%s", support_id, stage, status)


def finish_progress(progress: dict[str, Any]) -> None:
    """标记进度完成并保存"""
    progress["finished_at"] = datetime.now().isoformat()
    save_progress(progress["run_id"], progress)
    logger.info("进度完成 run_id=%s finished_at=%s", progress["run_id"], progress["finished_at"])


def find_latest_progress_file() -> str | None:
    """查找最新的进度文件 run_id"""
    logs_dir = settings.KBD_LOGS_DIR
    if not logs_dir.exists():
        return None

    progress_files = list(logs_dir.glob("progress_*.json"))
    if not progress_files:
        return None

    # 按文件名排序（包含时间戳），取最新的
    progress_files.sort(reverse=True)

    # 提取 run_id: progress_20260420_100000.json → 20260420_100000
    name = progress_files[0].stem
    run_id = name.replace("progress_", "")
    logger.debug("找到最新进度文件 run_id=%s", run_id)
    return run_id


def get_completed_ids_for_stage(progress: dict[str, Any], stage: str) -> list[str]:
    """获取某个 stage 已完成的案例 ID 列表（包含 done 和 skipped）"""
    stages_stats = progress.get("stages", {}).get(stage, {})
    completed = stages_stats.get("completed_ids", [])
    skipped = stages_stats.get("skipped_ids", [])
    return list(set(completed + skipped))


def get_case_stage_status(progress: dict[str, Any], support_id: str, stage: str) -> str:
    """获取某个案例在特定 stage 的状态"""
    return progress.get("cases", {}).get(support_id, {}).get(stage, "pending")


def is_case_done_for_stage(progress: dict[str, Any], support_id: str, stage: str) -> bool:
    """检查某个案例在特定 stage 是否已完成"""
    status = get_case_stage_status(progress, support_id, stage)
    return status in ("done", "skipped")