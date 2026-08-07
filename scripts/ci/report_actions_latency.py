#!/usr/bin/env python3
"""从 GitHub Actions runs API 输出中计算 CI 队列、执行和端到端时延分位数。"""

from __future__ import annotations

import json
import math
import sys
from collections import defaultdict
from datetime import datetime
from typing import Any


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def percentile(values: list[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def minutes(seconds: float) -> str:
    return f"{seconds / 60:.2f} min"


def flatten(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for page in payload for item in (page if isinstance(page, list) else [page])]
    return []


def main() -> int:
    runs = flatten(json.load(sys.stdin))
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for run in runs:
        if run.get("status") != "completed":
            continue
        created = parse_time(run.get("created_at"))
        started = parse_time(run.get("run_started_at"))
        updated = parse_time(run.get("updated_at"))
        if not created or not started or not updated:
            continue
        grouped[run.get("name", "未命名 workflow")].append(
            {
                "queue": (started - created).total_seconds(),
                "duration": (updated - started).total_seconds(),
                "total": (updated - created).total_seconds(),
                "conclusion": run.get("conclusion", "unknown"),
            }
        )

    print("# GitHub Actions CI 时延基线")
    print()
    print("统计口径：已完成 workflow run；队列时间为 `run_started_at - created_at`，执行时间为 `updated_at - run_started_at`。")
    print()
    print("| Workflow | 样本数 | 成功率 | Queue P50/P95 | 执行 P50/P95 | 端到端 P50/P95 |")
    print("|---|---:|---:|---|---|---|")
    for name, items in sorted(grouped.items()):
        success = sum(item["conclusion"] == "success" for item in items) / len(items) * 100
        queues = [item["queue"] for item in items]
        durations = [item["duration"] for item in items]
        totals = [item["total"] for item in items]
        print(
            f"| {name} | {len(items)} | {success:.1f}% | "
            f"{minutes(percentile(queues, .50))} / {minutes(percentile(queues, .95))} | "
            f"{minutes(percentile(durations, .50))} / {minutes(percentile(durations, .95))} | "
            f"{minutes(percentile(totals, .50))} / {minutes(percentile(totals, .95))} |"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
