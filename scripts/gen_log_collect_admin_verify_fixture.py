"""生成 admin-ui 真浏览器验收用的只读 fixture。

第一性原理：验收数据也必须由唯一派生点 ``derive()`` 产生，不能手写一份
「看起来像」的 display，否则截图证明的是 mock 而不是产品逻辑。

用法（仓库根）：
    uv run --frozen python scripts/gen_log_collect_admin_verify_fixture.py
    uv run --frozen python scripts/gen_log_collect_admin_verify_fixture.py --check
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "case-service"))

from app.domain.log_collect_display import derive  # noqa: E402

OUT = ROOT / "frontend" / "admin" / "verify" / "mock-admin.json"
NOW = datetime(2026, 11, 13, 9, 49, 30, tzinfo=UTC)


def build() -> dict:
    """Q2026111300155 上线首错：P1 已上传，等待 5888 发布规范归档名。"""
    task = {
        "task_id": "85d87375-91d1-4450-8b28-529b2b646180c",
        "case_id": "Q2026111300155",
        "client_id": None,
        "customer_name": None,
        "origin_node_ip": "192.168.3.10",
        "upload_mode": "acli_direct",
        "status": "completed",
        "progress": 100,
        "archive_size": None,
        "object_size": None,
        "delivery_status": "none",
        "relay_status": "none",
        "error_code": None,
        "error_message": None,
        "trace_id": "trace-Q2026111300155-001",
        "created_at": "2026-11-13T09:48:00Z",
        "updated_at": "2026-11-13T09:49:10Z",
        "finished_at": "2026-11-13T09:49:00Z",
        "delivery_finished_at": None,
        "detail": {
            "p1_archive_check": {
                "verified": False,
                "reason": "archive_missing",
                "checked_at": "2026-11-13T09:49:10Z",
            }
        },
    }
    derived = derive(task, now=NOW)
    row = {**task, **derived, "archive_check": task["detail"]["p1_archive_check"]}
    payload = {
        "list": {
            "items": [row],
            "total": 1,
            "page": 1,
            "page_size": 20,
            "summary": {
                "total": 1,
                "by_conclusion": {"pending": 1, "success": 0, "partial": 0, "failed": 0, "cancelled": 0},
            },
        },
        "detail": row,
        "transferMetrics": {"items": []},
        "networkProbe": {"items": [], "grade": "unknown", "grade_label": "未探测", "targets": []},
        "processLogs": {"items": []},
        "artifactManifest": {"items": []},
        "destinationProgress": {"state": "no_record", "path": "", "target_path": None},
    }
    # 等待态是本次改造的核心验收点，写死断言防止 fixture 悄悄漂移成旧口径。
    assert derived["display"]["phase"] == "verifying", derived["display"]["phase"]
    assert derived["display"]["fault_attribution"]["code"] == "LC-HTP-001"
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="仅校验现有 fixture 与派生结果一致")
    args = parser.parse_args()
    payload = build()
    if args.check:
        if not OUT.exists():
            print(f"fixture 不存在：{OUT}", file=sys.stderr)
            return 1
        if json.loads(OUT.read_text(encoding="utf-8")) != payload:
            print("fixture 与 derive() 结果不一致，请重新生成", file=sys.stderr)
            return 1
        print("fixture 与 derive() 一致")
        return 0
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"已写入 {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
