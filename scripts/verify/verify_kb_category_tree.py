#!/usr/bin/env python3
"""分类树（kb_category）漂移只读巡检：防"管理页可见、S0 不可选"的隐形分类。

背景（2026-10-10 Q2026101026818 复盘）：
    分类树叶子资格由"是否存在子节点"（leaf_only）决定，而下游 S0 另有叶子 code
    形态探针（前缀-纯数字结尾）。当中间层分组节点的子分类被删除/拖拽（管理页
    手工编辑漂移）后，它退化为叶子形态但 code 异常，曾被 S0 静默过滤成"隐形
    分类"；基线叶子（如 虚拟机-015~020）被删除后，整个故障语义族从 AI 候选中
    消失。本脚本以只读方式对比基线 YAML 与线上库，让漂移可观测、可门禁。

检查项（error 非零即漂移，warning 供对照）：
1. E1 隐形分类：DB 中无子节点（叶子形态）但 code 不符合叶子编码契约。
2. E2 基线漂移：基线 YAML 叶子 id 在库中缺失（本次事故根因形态）。
3. W1 基线叶子被停用（is_active=false）。
4. W2 基线叶子 path_labels 与库中不一致（被拖拽/重命名）。

不提供任何数据修复：数据修复一律走基线重导
（POST /api/kb/categories/import，upsert、code 不变、KBD 引用零破坏）。

用法：
    # 纯函数离线自检（CI 无库环境）
    uv run python scripts/verify/verify_kb_category_tree.py --self-test
    # 连库只读巡检（--baseline 默认 backend/kb-service/config/category_baseline.yaml）
    uv run python scripts/verify/verify_kb_category_tree.py --database-url "$DATABASE_URL"
    # 严格模式（error>0 非零退出，作发布/巡检门禁）
    uv run python scripts/verify/verify_kb_category_tree.py --database-url "$DATABASE_URL" --strict
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import uuid
from pathlib import Path
from typing import Any

# 唯一调用链：本次巡检执行的可追踪标识，输出到全部日志行
TRACE_ID = uuid.uuid4().hex

# 与 backend/shared/utils/category_code.py 的 LEAF_CODE_RE 保持一致
# （巡检脚本独立运行，不依赖 backend/shared）
LEAF_CODE_RE = re.compile(r"^[一-鿿A-Za-z0-9-]+-\d+$")


def log(level: str, message: str) -> None:
    """带调用链的结构化日志行。"""
    print(f"[{TRACE_ID}] {level}  {message}")


def is_leaf_code(code: object) -> bool:
    """判断 code 是否符合叶子编码契约（前缀-纯数字结尾）。"""
    return isinstance(code, str) and bool(LEAF_CODE_RE.match(code))


def load_baseline_leaves(baseline_path: Path) -> dict[str, dict[str, Any]]:
    """读取基线 YAML，返回 {叶子id: {label, path}} 映射（仅叶节点条目）。"""
    import yaml  # noqa: PLC0415

    with open(baseline_path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    leaves: dict[str, dict[str, Any]] = {}
    for cat in data.get("categories", []):
        code = str(cat.get("id") or "")
        if code:
            leaves[code] = {"label": str(cat.get("label") or ""), "path": list(cat.get("path") or [])}
    return leaves


def find_hidden_leaf_codes(nodes: list[dict[str, Any]]) -> list[str]:
    """纯函数：找出"无子节点（叶子形态）但 code 不合规"的隐形分类 code。

    仅审计活跃节点：停用节点被 leaf_only 查询显式排除，不进入 S0，不构成隐形。
    """
    parent_ids = {node.get("parent_id") for node in nodes if node.get("parent_id") is not None}
    return sorted(
        {
            str(node["code"])
            for node in nodes
            if node.get("code")
            and node.get("is_active") is not False
            and node.get("id") not in parent_ids
            and not is_leaf_code(node["code"])
        }
    )


def find_baseline_drift(
    baseline_leaves: dict[str, dict[str, Any]],
    db_nodes: list[dict[str, Any]],
) -> tuple[list[str], list[str], list[str]]:
    """纯函数：对比基线叶子与库中节点，返回 (缺失, 停用, path 不一致) 三个列表。"""
    by_code: dict[str, dict[str, Any]] = {}
    for node in db_nodes:
        code = str(node.get("code") or "")
        if code:
            by_code.setdefault(code, node)

    missing: list[str] = []
    inactive: list[str] = []
    path_mismatch: list[str] = []
    for code, meta in baseline_leaves.items():
        node = by_code.get(code)
        if node is None:
            missing.append(code)
            continue
        if node.get("is_active") is False:
            inactive.append(code)
        db_path = node.get("path_labels")
        if isinstance(db_path, str):
            try:
                db_path = json.loads(db_path)
            except json.JSONDecodeError:
                db_path = None
        if db_path is not None and list(db_path) != list(meta["path"]):
            path_mismatch.append(code)
    return sorted(missing), sorted(inactive), sorted(path_mismatch)


def _fetch_nodes(database_url: str) -> list[dict[str, Any]]:
    """连库只读取全量节点（独立事件循环，脚本场景最简生命周期）。"""
    import asyncpg  # noqa: PLC0415

    async def _inner() -> list[dict[str, Any]]:
        pool = await asyncpg.create_pool(database_url, min_size=1, max_size=2)
        try:
            rows = await pool.fetch("SELECT id, code, parent_id, is_active, path_labels FROM kb_category")
            return [dict(row) for row in rows]
        finally:
            await pool.close()

    return asyncio.run(_inner())


def run_self_test() -> int:
    """纯函数离线自检：覆盖隐形分类识别与基线漂移判定。"""
    nodes = [
        {"id": 1, "code": "虚拟机-L1", "parent_id": None, "is_active": True, "path_labels": ["虚拟机"]},
        {
            "id": 2,
            "code": "虚拟机-014",
            "parent_id": 1,
            "is_active": True,
            "path_labels": ["虚拟机", "虚拟机scmt迁移失败"],
        },
        {
            "id": 3,
            "code": "虚拟机-L2-虚拟机集群内或跨集群迁移失败",
            "parent_id": 1,
            "is_active": True,
            "path_labels": ["虚拟机", "虚拟机集群内或跨集群迁移失败"],
        },
        # 停用的空壳分组节点：被 leaf_only 显式排除，不构成隐形（staging id=29 先例）
        {
            "id": 4,
            "code": "硬件-L2-客户机硬件",
            "parent_id": None,
            "is_active": False,
            "path_labels": ["硬件", "客户机硬件"],
        },
    ]
    hidden = find_hidden_leaf_codes(nodes)
    assert hidden == ["虚拟机-L2-虚拟机集群内或跨集群迁移失败"], hidden

    baseline = {
        "虚拟机-015": {
            "label": "虚拟机集群内热迁移失败（跨存储）",
            "path": ["虚拟机", "虚拟机集群内或跨集群迁移失败", "虚拟机集群内热迁移失败（跨存储）"],
        },
        "虚拟机-017": {
            "label": "虚拟机跨集群热迁移失败",
            "path": ["虚拟机", "虚拟机集群内或跨集群迁移失败", "虚拟机跨集群热迁移失败"],
        },
    }
    missing, inactive, mismatch = find_baseline_drift(baseline, nodes)
    assert missing == ["虚拟机-015", "虚拟机-017"], missing
    assert inactive == [] and mismatch == []

    drifted = [
        {
            "id": 2,
            "code": "虚拟机-017",
            "parent_id": 1,
            "is_active": False,
            "path_labels": ["虚拟机", "其它分组", "虚拟机跨集群热迁移失败"],
        }
    ]
    missing2, inactive2, mismatch2 = find_baseline_drift({"虚拟机-017": baseline["虚拟机-017"]}, drifted)
    assert missing2 == [] and inactive2 == ["虚拟机-017"] and mismatch2 == ["虚拟机-017"]

    log("INFO", "self-test 通过：隐形分类识别 / 基线缺失 / 停用 / path 漂移判定均符合预期")
    return 0


def run_db_audit(database_url: str, baseline_path: Path, *, strict: bool) -> int:
    """连库只读巡检；strict 模式下存在 error 时返回非零退出码。"""
    nodes = _fetch_nodes(database_url)
    log("INFO", f"已读取库中分类节点 {len(nodes)} 条")

    baseline_leaves = load_baseline_leaves(baseline_path)
    log("INFO", f"已读取基线叶节点 {len(baseline_leaves)} 条（{baseline_path}）")

    errors: list[str] = []
    warnings: list[str] = []

    for code in find_hidden_leaf_codes(nodes):
        errors.append(f"E1 隐形分类：{code} 无子节点但 code 不符合叶子编码契约（S0 候选不可见）")

    missing, inactive, path_mismatch = find_baseline_drift(baseline_leaves, nodes)
    for code in missing:
        errors.append(f"E2 基线漂移：基线叶节点 {code} 在库中缺失（该故障语义族从 AI 候选消失）")
    for code in inactive:
        warnings.append(f"W1 基线叶节点 {code} 在库中为停用状态")
    for code in path_mismatch:
        warnings.append(f"W2 基线叶节点 {code} 的 path_labels 与基线不一致（被拖拽/重命名）")

    for item in warnings:
        log("WARN", item)
    for item in errors:
        log("ERROR", item)

    log("INFO", f"巡检完成：error={len(errors)} warning={len(warnings)}")
    if errors and strict:
        log("ERROR", "strict 模式：存在漂移错误，以非零退出（修复方式见脚本 docstring：基线重导）")
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="分类树漂移只读巡检（防隐形分类）")
    parser.add_argument("--database-url", type=str, default=None, help="PostgreSQL 连接串（默认读 DATABASE_URL）")
    parser.add_argument(
        "--baseline",
        type=Path,
        default=Path(__file__).resolve().parents[2] / "backend" / "kb-service" / "config" / "category_baseline.yaml",
        help="基线 YAML 路径（默认仓库内 category_baseline.yaml）",
    )
    parser.add_argument("--self-test", action="store_true", help="纯函数离线自检（不连库）")
    parser.add_argument("--strict", action="store_true", help="error>0 时非零退出（巡检/发布门禁）")
    args = parser.parse_args()

    if args.self_test:
        return run_self_test()

    database_url = args.database_url or os.environ.get("DATABASE_URL")
    if not database_url:
        log("ERROR", "未提供 --database-url，且环境变量 DATABASE_URL 为空")
        return 2
    return run_db_audit(database_url, args.baseline, strict=args.strict)


if __name__ == "__main__":
    sys.exit(main())
