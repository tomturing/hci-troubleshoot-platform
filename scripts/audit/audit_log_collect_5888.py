#!/usr/bin/env python3
"""只读巡检：把「失败」工单与 5888 的真实归档事实对账。

输入为 JSON 数组，每项至少含 case_id，可选 created_at（ISO8601）与
expected_size（字节）：

    [
      {"case_id": "Q2026111900236", "created_at": "...", "expected_size": 0}
    ]

四类判定与平台 domain.log_collect_archive.judge_archive 同口径：
    verified       本轮归档已落盘（体积够、时间对得上）
    stale_archive  只有更早的历史包，不能当成本轮送达
    size_mismatch  时间对但体积小于记录值，疑似截断
    missing        5888 明确回 found=false

只读约束：只 GET /archive_status，不改平台状态、不写数据库、不打印删除命令。
每次请求带唯一 X-Request-ID，日志与输出中可追踪调用链。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

LOOKBACK_SECONDS = 300


def fetch_token(base_url: str, explicit: str | None) -> str:
    """优先使用传入 token，否则从 5888 首页 meta 读取（首页 meta 不是鉴权本身）。"""
    if explicit:
        return explicit
    with urllib.request.urlopen(base_url + "/", timeout=10) as response:
        html = response.read().decode("utf-8", errors="replace")
    match = re.search(r'name="upload-token" content="([^"]+)"', html)
    if not match:
        raise RuntimeError("5888 首页未找到 upload-token meta，请显式传 --token")
    return match.group(1)


def verdict_of(item: dict[str, Any], archive: dict[str, Any] | None) -> str:
    """与平台归档判据一致：先看存在性，再看时间，最后看体积。"""
    if not archive or not archive.get("found"):
        return "missing"
    size = archive.get("size")
    if not isinstance(size, int) or size <= 0:
        return "missing"
    created = item.get("created_at")
    archived_at = None
    mtime = archive.get("mtime")
    if isinstance(mtime, (int, float)):
        archived_at = datetime.fromtimestamp(int(mtime), tz=datetime.UTC)
    if created and archived_at:
        try:
            created_at = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=datetime.UTC)
            if archived_at < created_at - timedelta(seconds=LOOKBACK_SECONDS):
                return "stale_archive"
        except ValueError:
            pass
    expected = item.get("expected_size")
    if isinstance(expected, int) and expected > 0 and size < expected:
        return "size_mismatch"
    return "verified"


def main() -> int:
    parser = argparse.ArgumentParser(description="失败工单 × 5888 归档事实只读对账")
    parser.add_argument("--input", required=True, type=Path, help="JSON 数组文件，- 表示 stdin")
    parser.add_argument("--base-url", default=os.getenv("LOG_COLLECT_RELAY_URL", "http://192.168.0.2:5888"))
    parser.add_argument("--token", default=os.getenv("LOG_COLLECT_RELAY_TOKEN"))
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--output", type=Path, help="另存完整 JSON 结果")
    args = parser.parse_args()

    raw = sys.stdin.read() if str(args.input) == "-" else args.input.read_text(encoding="utf-8")
    items = json.loads(raw)
    if not isinstance(items, list):
        raise ValueError("输入必须是 JSON 数组")

    token = fetch_token(args.base_url, args.token)

    def check(item: dict[str, Any]) -> dict[str, Any]:
        case_id = str(item["case_id"])
        trace_id = f"lc-5888-audit-{uuid.uuid4().hex[:16]}"
        url = f"{args.base_url}/archive_status?filename={case_id}.zip"
        request = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {token}", "X-Request-ID": trace_id},
        )
        archive: dict[str, Any] | None
        error = None
        try:
            with urllib.request.urlopen(request, timeout=args.timeout) as response:
                archive = json.loads(response.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - 观测失败只记录，不能冒充「没包」
            archive = None
            error = str(exc)
        result = {
            "case_id": case_id,
            "trace_id": trace_id,
            "verdict": "query_error" if error else verdict_of(item, archive),
            "error": error,
            "archive": archive,
        }
        print(
            f"trace={trace_id} case={case_id} verdict={result['verdict']}",
            file=sys.stderr,
        )
        return result

    with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
        results = list(pool.map(check, items))

    counts = Counter(row["verdict"] for row in results)
    print("\n=== 5888 归档事实对账 ===")
    for key in ("verified", "stale_archive", "size_mismatch", "missing", "query_error"):
        print(f"{key}: {counts.get(key, 0)}")
    verified = [row["case_id"] for row in results if row["verdict"] == "verified"]
    print("本轮已送达工单：" + (", ".join(verified) if verified else "无"))
    print("注意：verified 只说明 5888 有包；手工上传/后续重试需再与任务证据交叉核对。")

    if args.output:
        args.output.write_text(
            json.dumps(results, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return 0 if not counts.get("query_error") else 1


if __name__ == "__main__":
    raise SystemExit(main())
