#!/usr/bin/env python3
"""根因信号闭环存量只读审计：全库扫描「非 closed 的 executable」与「有拒绝候选」的 KBD。

背景（根因抽取闭环方案 §7/§8）：
    executable 根因必须现场可执行（requires 全覆盖 + 无写死实体 + 能力具备）方判 closed；
    否则三态降级（dangling_requires / hardcoded_literals / capability_missing）并被抽取/编辑
    端拆入 root_signal_rejected_json（不进 root_signal_json、不自动降级 text）。PR-1~PR-4 只对新
    抽取/编辑增量生效，历史存量 KBD 仍可能残留「未评估」或「非 closed」的可执行根因。本脚本以
    **纯只读**方式量化这一存量欠账，供回刷排期与发布门禁观测，不提供任何数据修复。

审计口径（与 backend/kb-service/app/services/root_signal_closure.py 四态同源，脚本内独立钉值）：
    closed                已闭环（现场可执行）——健康
    dangling_requires     变量悬空无来源——非 closed（问题项）
    hardcoded_literals    含写死实体未参数化——非 closed（问题项）
    capability_missing    采集能力缺失——非 closed（问题项）
    executable_unassessed executable 但 closure_status 缺失（PR 前老数据未评估）——待回刷项
    rejected_present      KBD 的 root_signal_rejected_json 非空（有被拒候选留痕）——待处置项

不提供任何数据修复：整改一律走「重新抽取根因信号」/「逐条编辑」端点（PR-1~PR-4 已接入闭包裁决）。

用法：
    # 纯函数离线自检（CI 无库环境，钉四态归类与拒绝统计的正确性）
    uv run python scripts/verify/verify_root_signal_closure.py --self-test
    # 连库只读审计（默认扫 draft/published/unpublishable，跳过 archived/rejected）
    uv run python scripts/verify/verify_root_signal_closure.py --database-url "$DATABASE_URL"
    # 严格模式（存在非 closed executable 或有拒绝候选即非零退出，作巡检/发布门禁）
    uv run python scripts/verify/verify_root_signal_closure.py --database-url "$DATABASE_URL" --strict
    # 导出明细 CSV（不改数据，仅落问题清单便于排期）
    uv run python scripts/verify/verify_root_signal_closure.py --database-url "$DATABASE_URL" --csv out.csv
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

# 唯一调用链：本次审计执行的可追踪标识，输出到全部日志行
TRACE_ID = uuid.uuid4().hex

# 闭包四态字面量（与 root_signal_closure.py 同源；脚本独立运行不 import backend，故此处钉值）
CLOSURE_CLOSED = "closed"
CLOSURE_PROBLEM_STATES = frozenset({"dangling_requires", "hardcoded_literals", "capability_missing"})
# executable 但无 closure_status（PR 前老数据）：既非 closed 也非已知问题态，单列为「待回刷」
EXECUTABLE_UNASSESSED = "executable_unassessed"


def log(level: str, message: str) -> None:
    """带调用链的结构化日志行。"""
    print(f"[{TRACE_ID}] {level}  {message}")


def _as_list(raw: Any) -> list[dict[str, Any]]:
    """归一 JSONB 值为对象数组：asyncpg 可能回 str，None/标量兜底空数组。"""
    if raw is None:
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return []
    if isinstance(raw, dict):
        return [raw]
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    return []


def classify_executable_signal(sig: dict[str, Any]) -> str | None:
    """单条根因信号 → 可执行根因的闭包归类；text 形态返回 None（无闭包义务，不计入）。

    返回 closed / dangling_requires / hardcoded_literals / capability_missing /
    executable_unassessed（executable 但 closure_status 缺失）。
    """
    if str(sig.get("match_type") or "") != "executable":
        return None
    status = sig.get("closure_status")
    if status == CLOSURE_CLOSED:
        return CLOSURE_CLOSED
    if isinstance(status, str) and status in CLOSURE_PROBLEM_STATES:
        return status
    # 缺失 / 未知态：PR 前未评估的老数据，归为待回刷
    return EXECUTABLE_UNASSESSED


def scan_rows(rows: list[dict[str, Any]]) -> tuple[dict[str, int], list[dict[str, Any]]]:
    """纯函数：扫描 KBD 行，产出 (统计计数, 问题明细列表)。不做任何写库。

    统计键：
      kbd_scanned                扫描的 KBD 总数
      executable_signals         可执行根因信号总数
      closed                     其中已闭环数
      dangling_requires / hardcoded_literals / capability_missing  各问题态计数
      executable_unassessed      可执行但未评估（closure_status 缺失）计数
      non_closed_executables     非 closed 的可执行根因总数（问题态 + 未评估）
      rejected_kbd_count         有拒绝候选（root_signal_rejected_json 非空）的 KBD 数
      rejected_signal_count      拒绝候选条数合计
    明细每行：{kbd_id, category_id, status, kind, signal_text, detail}
    """
    stats: dict[str, int] = {
        "kbd_scanned": 0,
        "executable_signals": 0,
        "closed": 0,
        "dangling_requires": 0,
        "hardcoded_literals": 0,
        "capability_missing": 0,
        EXECUTABLE_UNASSESSED: 0,
        "non_closed_executables": 0,
        "rejected_kbd_count": 0,
        "rejected_signal_count": 0,
    }
    detail: list[dict[str, Any]] = []

    for row in rows:
        stats["kbd_scanned"] += 1
        kbd_id = row.get("id")
        category_id = row.get("category_id")
        status = row.get("status")

        for sig in _as_list(row.get("root_signal_json")):
            kind = classify_executable_signal(sig)
            if kind is None:
                continue  # text 形态不计
            stats["executable_signals"] += 1
            stats[kind] = stats.get(kind, 0) + 1
            if kind != CLOSURE_CLOSED:
                stats["non_closed_executables"] += 1
                unresolved = sig.get("requires") or sig.get("unresolved") or []
                detail.append(
                    {
                        "kbd_id": kbd_id,
                        "category_id": category_id,
                        "status": status,
                        "kind": kind,
                        "signal_text": sig.get("signal_text") or "",
                        "detail": json.dumps(
                            {"closure_status": sig.get("closure_status"), "requires": unresolved},
                            ensure_ascii=False,
                        ),
                    }
                )

        rejected = _as_list(row.get("root_signal_rejected_json"))
        if rejected:
            stats["rejected_kbd_count"] += 1
            stats["rejected_signal_count"] += len(rejected)
            for rj in rejected:
                cand = rj.get("candidate") if isinstance(rj.get("candidate"), dict) else {}
                detail.append(
                    {
                        "kbd_id": kbd_id,
                        "category_id": category_id,
                        "status": status,
                        "kind": "rejected_present",
                        "signal_text": (cand or {}).get("signal_text") or "",
                        "detail": json.dumps(
                            {
                                "reason_code": rj.get("reason_code"),
                                "closure_status": rj.get("closure_status"),
                                "unresolved": rj.get("unresolved") or [],
                            },
                            ensure_ascii=False,
                        ),
                    }
                )

    return stats, detail


def _fetch_rows(database_url: str, statuses: list[str], limit: int | None) -> list[dict[str, Any]]:
    """连库只读取数（独立事件循环，脚本场景最简生命周期）。绝不写库。"""
    import asyncpg  # noqa: PLC0415

    sql = (
        "SELECT id, category_id, status, root_signal_json, root_signal_rejected_json "
        "FROM kbd_entry WHERE status = ANY($1::text[]) ORDER BY id"
    )

    async def _inner() -> list[dict[str, Any]]:
        pool = await asyncpg.create_pool(database_url, min_size=1, max_size=2)
        try:
            if limit is not None:
                rows = await pool.fetch(sql + " LIMIT $2", statuses, limit)
            else:
                rows = await pool.fetch(sql, statuses)
            return [dict(row) for row in rows]
        finally:
            await pool.close()

    return asyncio.run(_inner())


def _write_csv(path: Path, detail: list[dict[str, Any]]) -> None:
    """落问题明细 CSV（UTF-8 BOM，Excel 直开不乱码）。"""
    cols = ["kbd_id", "category_id", "status", "kind", "signal_text", "detail"]
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=cols)
        writer.writeheader()
        writer.writerows(detail)


def run_db_audit(database_url: str, statuses: list[str], *, strict: bool, limit: int | None, csv_path: Path | None) -> int:
    """连库只读审计；strict 下存在非 closed executable 或拒绝候选时非零退出。"""
    rows = _fetch_rows(database_url, statuses, limit)
    log("INFO", f"已读取 KBD 行 {len(rows)} 条（status ∈ {','.join(statuses)}{'，limit=' + str(limit) if limit else ''}）")

    stats, detail = scan_rows(rows)

    log("INFO", f"可执行根因 {stats['executable_signals']} 条：closed={stats['closed']}")
    for state in ("dangling_requires", "hardcoded_literals", "capability_missing", EXECUTABLE_UNASSESSED):
        if stats.get(state):
            log("WARN", f"非 closed 可执行根因 {state}：{stats[state]} 条")
    log("INFO", f"非 closed 可执行根因合计 {stats['non_closed_executables']} 条")
    log("INFO", f"有拒绝候选 KBD {stats['rejected_kbd_count']} 篇，拒绝候选合计 {stats['rejected_signal_count']} 条")

    if csv_path is not None:
        _write_csv(csv_path, detail)
        log("INFO", f"问题明细已导出 {len(detail)} 行 → {csv_path}")

    has_debt = stats["non_closed_executables"] > 0 or stats["rejected_kbd_count"] > 0
    if has_debt and strict:
        log("ERROR", "strict 模式：存在非 closed 可执行根因或有拒绝候选，以非零退出（整改方式见脚本 docstring）")
        return 1
    return 0


def run_self_test() -> int:
    """纯函数离线自检：钉四态归类、text 排除、未评估、拒绝候选计数与明细的正确性。"""
    rows = [
        # 一篇健康：executable closed
        {
            "id": 1,
            "category_id": "虚拟机-010",
            "status": "published",
            "root_signal_json": [{"match_type": "executable", "signal_text": "closed 锚", "closure_status": CLOSURE_CLOSED}],
            "root_signal_rejected_json": None,
        },
        # 一篇问题：dangling + hardcoded；text 不计入；一条无 closure_status 归未评估
        {
            "id": 2,
            "category_id": "虚拟机-011",
            "status": "draft",
            "root_signal_json": [
                {"match_type": "executable", "signal_text": "悬空", "closure_status": "dangling_requires", "requires": ["node_ip"]},
                {"match_type": "executable", "signal_text": "写死", "closure_status": "hardcoded_literals"},
                {"match_type": "executable", "signal_text": "未评估"},
                {"match_type": "text", "signal_text": "关键词锚", "closure_status": None},
            ],
            "root_signal_rejected_json": [],
        },
        # 一篇有拒绝候选（JSONB 以字符串返回的兼容路径）
        {
            "id": 3,
            "category_id": "虚拟机-012",
            "status": "unpublishable",
            "root_signal_json": [],
            "root_signal_rejected_json": json.dumps(
                [{"reason_code": "ROOT_CLOSURE_CAPABILITY_MISSING", "closure_status": "capability_missing", "candidate": {"signal_text": "缺能力"}}],
                ensure_ascii=False,
            ),
        },
    ]

    # 单条归类
    assert classify_executable_signal({"match_type": "text"}) is None
    assert classify_executable_signal({"match_type": "executable", "closure_status": CLOSURE_CLOSED}) == CLOSURE_CLOSED
    assert classify_executable_signal({"match_type": "executable", "closure_status": "dangling_requires"}) == "dangling_requires"
    assert classify_executable_signal({"match_type": "executable"}) == EXECUTABLE_UNASSESSED

    stats, detail = scan_rows(rows)
    assert stats["kbd_scanned"] == 3, stats
    # executable：closed(第1篇 1 条)+dangling+hardcoded+未评估(第2篇 3 条)+第3篇 root_signal_json 空 0 条 = 4
    assert stats["executable_signals"] == 4, stats
    assert stats["closed"] == 1, stats
    assert stats["dangling_requires"] == 1 and stats["hardcoded_literals"] == 1, stats
    assert stats[EXECUTABLE_UNASSESSED] == 1, stats
    assert stats["non_closed_executables"] == 3, stats
    assert stats["rejected_kbd_count"] == 1 and stats["rejected_signal_count"] == 1, stats

    # 明细：第2篇 3 条非 closed + 第3篇 1 条 rejected_present = 4
    assert len(detail) == 4, detail
    kinds = {d["kind"] for d in detail}
    assert kinds == {"dangling_requires", "hardcoded_literals", EXECUTABLE_UNASSESSED, "rejected_present"}, kinds
    assert any(d["kbd_id"] == 3 and d["detail"].find("CAPABILITY") >= 0 for d in detail), detail

    # 全空/非法输入不炸
    s0, d0 = scan_rows([])
    assert s0["kbd_scanned"] == 0 and not d0

    log("INFO", "self-test 通过：四态归类 / text 排除 / 未评估 / 拒绝候选计数与明细均符合预期")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="根因信号闭环存量只读审计（非 closed executable + 拒绝候选）")
    parser.add_argument("--database-url", type=str, default=None, help="PostgreSQL 连接串（默认读 DATABASE_URL）")
    parser.add_argument(
        "--statuses",
        type=str,
        default="draft,published,unpublishable",
        help="纳入审计的 KBD 状态（逗号分隔，默认 draft,published,unpublishable；archived/rejected 不参与线上定位故排除）",
    )
    parser.add_argument("--limit", type=int, default=None, help="仅扫描前 N 条（抽样排查用；默认全库）")
    parser.add_argument("--csv", type=Path, default=None, help="导出问题明细 CSV（不改数据）")
    parser.add_argument("--self-test", action="store_true", help="纯函数离线自检（不连库）")
    parser.add_argument("--strict", action="store_true", help="存在非 closed executable 或拒绝候选时非零退出（巡检/发布门禁）")
    args = parser.parse_args()

    if args.self_test:
        return run_self_test()

    database_url = args.database_url or os.environ.get("DATABASE_URL")
    if not database_url:
        log("ERROR", "未提供 --database-url，且环境变量 DATABASE_URL 为空")
        return 2
    statuses = [s.strip() for s in args.statuses.split(",") if s.strip()]
    if not statuses:
        log("ERROR", "--statuses 解析为空")
        return 2
    return run_db_audit(database_url, statuses, strict=args.strict, limit=args.limit, csv_path=args.csv)


if __name__ == "__main__":
    sys.exit(main())
