#!/usr/bin/env python3
"""存量根因信号「重新过门禁」工具（L2 充分性 + L3 最少性 → 运行时准入）。

背景（第一性原理）：
    运行时只接受 `provenance.gate_version >= 2 且 gate_status == 'passed'` 的根因信号
    （见 kb-service/app/routes/playbooks.py 的准入闸门）。存量信号由 v1 抽取产生，
    provenance 里没有 gate_version，因此**在重抽之前永远不会参与判定**——既有资产
    无法贡献分流收益。本工具对存量信号**重跑同一套确定性门禁**并盖章，令其可准入。

诚实边界：
    本工具只重跑确定性规则（通用错误串 / argv-only 命令 / 配置字段恒真 / 冗余蕴含），
    不做语义重判、不调用 LLM、不改写 signal_text/evidence/match 内容。因此：
    - 拒收（sufficiency_violation）与剪枝（ redundancy）是可审计的确定性结论；
    - 「疑似恒真」（warning）只降级为人工复核，**绝不给运行时准入**（gate_status=warning）。
    语义层面的复核仍由人工在审核页完成。

用法：
    # 干跑（只打印将发生的变化，不写库）
    PYTHONPATH=backend:backend/shared:backend/kb-service \\
      .venv/bin/python scripts/ops/regate_root_signals.py --dsn "$DSN"

    # 落库
    ... --apply

幂等：已 gate_version>=2 的信号原样保留；重复执行不会二次剪枝。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
for _p in (str(REPO), str(REPO / "backend"), str(REPO / "backend" / "kb-service")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import asyncpg  # noqa: E402
from app.services.root_signal_extractor import (  # noqa: E402
    SUFFICIENCY_GATE_VERSION,
    gate_minimality,
    gate_sufficiency,
)

# DSN 只从环境变量取：凭据不得进仓库（脚本会被 CI 与其它环境复用，硬编码口令等于凭据泄漏）。
DEFAULT_DSN = os.environ.get("REGATE_DSN") or os.environ.get("DATABASE_URL") or ""


def _as_list(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    data = json.loads(value) if isinstance(value, str) else value
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def _already_gated(signal: dict[str, Any]) -> bool:
    try:
        return int((signal.get("provenance") or {}).get("gate_version") or 0) >= SUFFICIENCY_GATE_VERSION
    except (TypeError, ValueError):
        return False


def regate(signals: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """对一批已归一化的根因信号重跑 L2/L3。

    Returns:
        (kept, dropped, pruned)：dropped/pruned 均为可审计条目（含 reason_code）。

    最少性只作用于「本次新盖章」的信号：已 gated 的信号视为人工已确认，不再被
    后续重跑剪掉（幂等 + 尊重人工判断）。输出保持调用方原序。
    """
    stamped: dict[int, dict[str, Any]] = {}
    dropped: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    for signal in signals:
        if _already_gated(signal):
            stamped[id(signal)] = signal
            continue
        violation, warnings = gate_sufficiency(signal)
        if violation:
            dropped.append(
                {
                    "signal_text": str(signal.get("signal_text") or ""),
                    "reason_code": "insufficient_discrimination",
                    "reason": violation,
                }
            )
            continue
        provenance = dict(signal.get("provenance") or {})
        provenance["gate_version"] = SUFFICIENCY_GATE_VERSION
        provenance["gate_status"] = "warning" if warnings else "passed"
        provenance["gate_warnings"] = warnings
        stamped[id(signal)] = {**signal, "provenance": provenance}
        pending.append(signal)

    kept_pending, pruned = gate_minimality(pending)
    kept_pending_ids = {id(item) for item in kept_pending}
    pruned_ids = {id(item) for item in pending} - kept_pending_ids

    final: list[dict[str, Any]] = []
    for signal in signals:
        if id(signal) not in stamped or id(signal) in pruned_ids:
            continue
        final.append(stamped[id(signal)])
    return final, dropped, pruned


async def _run(dsn: str, apply: bool) -> int:
    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch(
            """
            SELECT id, support_id, root_signal_json, root_signal_rejected_json
            FROM kbd_entry
            WHERE root_signal_json IS NOT NULL AND jsonb_array_length(root_signal_json) > 0
            ORDER BY id
            """
        )
        print(f"[范围] 含根因信号的 KBD：{len(rows)} 篇")
        changed_rows = 0
        total_dropped = total_pruned = 0
        for row in rows:
            signals = _as_list(row["root_signal_json"])
            prior_rejected = _as_list(row["root_signal_rejected_json"])
            kept, dropped, pruned = regate(signals)
            total_dropped += len(dropped)
            total_pruned += len(pruned)
            before = json.dumps(signals, ensure_ascii=False, sort_keys=True)
            after = json.dumps(kept, ensure_ascii=False, sort_keys=True)
            if before == after and not dropped:
                print(f"  - kbd={row['id']} support={row['support_id']} 无需变更（已准入或零变化）")
                continue
            changed_rows += 1
            status = [str(((s.get("provenance") or {}).get("gate_status")) or "-") for s in kept]
            print(
                f"  * kbd={row['id']} support={row['support_id']} 保留={len(kept)} "
                f"拒收={len(dropped)} 剪枝={len(pruned)} gate_status={status}"
            )
            for item in dropped:
                print(f"      拒收: {item['signal_text'][:36]}… ← {item['reason'][:80]}")
            for item in pruned:
                print(f"      剪枝: {item['signal_text'][:36]}… ⊂ {item.get('redundant_with', '')[:36]}")
            if not apply:
                continue
            # 拒收与剪枝一并写入拒绝审计列：新增的 L3 门禁不能静默删信号。
            merged_rejected = prior_rejected + dropped + pruned
            await conn.execute(
                """
                UPDATE kbd_entry
                SET root_signal_json = CAST($2 AS jsonb),
                    root_signal_rejected_json = CAST($3 AS jsonb),
                    updated_at = now()
                WHERE id = $1
                """,
                row["id"],
                json.dumps(kept, ensure_ascii=False),
                json.dumps(merged_rejected, ensure_ascii=False),
            )
        print(
            f"[汇总] 变更 {changed_rows} 篇 / 拒收 {total_dropped} 条 / 剪枝 {total_pruned} 条 "
            f"（{'已落库' if apply else '干跑，未落库'}）"
        )
        return 0
    finally:
        await conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="存量根因信号重跑 L2/L3 门禁并盖章准入")
    parser.add_argument("--dsn", default=DEFAULT_DSN, help="PostgreSQL DSN（缺省读 REGATE_DSN / DATABASE_URL）")
    parser.add_argument("--apply", action="store_true", help="真正写库（默认干跑）")
    args = parser.parse_args()
    if not args.dsn:
        parser.error("缺少数据库 DSN：请传 --dsn 或设置 REGATE_DSN / DATABASE_URL 环境变量")
    return asyncio.run(_run(args.dsn, args.apply))


if __name__ == "__main__":
    raise SystemExit(main())
