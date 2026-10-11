#!/usr/bin/env python3
"""从线上 DB 重生成 category_baseline.yaml（v2.0 全树快照）。

用途：拖拽重排即时物化 code 后，用本脚本把 kb_category 现状导出为权威基线文件并提交，
使「文件 = DB 快照」这一事实源约定可持续，消除人工维护导致的漂移。

生成逻辑复用 `shared.utils.category_baseline.render_baseline_yaml`——与 `/export`
接口同一事实源，产出字节一致、可复现。

用法：
    uv run python scripts/refresh_category_baseline.py --database-url "$DATABASE_URL"
    uv run python scripts/refresh_category_baseline.py --database-url "$DATABASE_URL" --dry-run
    uv run python scripts/refresh_category_baseline.py --self-test
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

# 让脚本能 import backend/shared 下的共享纯函数（与 /export 单一事实源）
_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "backend"))

from shared.utils.category_baseline import render_baseline_yaml  # noqa: E402

TRACE_ID = uuid.uuid4().hex
DEFAULT_OUTPUT = _ROOT / "backend" / "kb-service" / "config" / "category_baseline.yaml"
# 排除停用节点？基线只收录活跃节点（is_active=TRUE）：软删除节点不进权威基线
_SELECT_SQL = (
    "SELECT id, code, name, domain, level, parent_id, path_labels "
    "FROM kb_category WHERE code IS NOT NULL AND is_active = TRUE"
)


def log(level: str, message: str) -> None:
    """带唯一调用链的结构化日志行。"""
    print(f"[{TRACE_ID}] {level}  {message}")


def _parse_path(raw: Any) -> list[str]:
    """path_labels 可能是 JSONB 对象或字符串，统一归一为 list[str]。"""
    if raw is None:
        return []
    if isinstance(raw, str):
        try:
            return list(json.loads(raw))
        except json.JSONDecodeError:
            return []
    return list(raw)


def fetch_nodes(database_url: str) -> list[dict[str, Any]]:
    """连库读取活跃分类节点，转为 render_baseline_yaml 入参（parent 解析为父 code）。"""
    import asyncpg  # noqa: PLC0415

    async def _inner() -> list[dict[str, Any]]:
        conn = await asyncpg.connect(database_url)
        try:
            rows = await conn.fetch(_SELECT_SQL)
        finally:
            await conn.close()

        records = [dict(r) for r in rows]
        id_to_code = {r["id"]: r["code"] for r in records}
        return [
            {
                "code": r["code"],
                "name": r["name"],
                "domain": r["domain"],
                "level": r["level"],
                "parent": id_to_code.get(r["parent_id"]) if r["parent_id"] is not None else None,
                "path": _parse_path(r["path_labels"]),
            }
            for r in records
        ]

    return asyncio.run(_inner())


def run_self_test() -> int:
    """离线自检：验证渲染逻辑（排序、parent 解析、头部字段），不连库。"""
    nodes = [
        {"code": "虚拟机-002", "name": "热迁移", "domain": "虚拟机", "level": 3, "parent": "虚拟机-001", "path": ["虚拟机", "迁移失败", "热迁移"]},
        {"code": "虚拟机-L1", "name": "虚拟机", "domain": "虚拟机", "level": 1, "parent": None, "path": ["虚拟机"]},
        {"code": "虚拟机-001", "name": "迁移失败", "domain": "虚拟机", "level": 2, "parent": "虚拟机-L1", "path": ["虚拟机", "迁移失败"]},
    ]
    out = render_baseline_yaml(nodes, generated="2026-10-11")
    assert 'version: "2.0"' in out, out
    assert "total: 3" in out, out
    assert out.index("虚拟机-L1") < out.index("虚拟机-001") < out.index("虚拟机-002"), "应按 域→层级→code 排序"
    log("INFO", "self-test 通过：v2.0 全树渲染 / 排序 / 头部字段符合预期")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="从线上 DB 重生成 category_baseline.yaml（v2.0 全树快照）")
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL"), help="PostgreSQL 连接串（默认取 $DATABASE_URL）")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="输出文件路径")
    parser.add_argument("--dry-run", action="store_true", help="仅打印，不落盘")
    parser.add_argument("--self-test", action="store_true", help="纯函数离线自检（不连库）")
    args = parser.parse_args()

    if args.self_test:
        return run_self_test()

    if not args.database_url:
        log("ERROR", "缺少 --database-url（或设置 $DATABASE_URL）")
        return 2

    nodes = fetch_nodes(args.database_url)
    log("INFO", f"已读取活跃分类节点 {len(nodes)} 条")
    yaml_content = render_baseline_yaml(nodes)

    if args.dry_run:
        log("INFO", f"--dry-run：将写入 {args.output}（{len(nodes)} 节点，{len(yaml_content)} 字节），本次不落盘")
        return 0

    args.output.write_text(yaml_content, encoding="utf-8")
    log("INFO", f"已写入基线快照：{args.output}（{len(nodes)} 节点）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
