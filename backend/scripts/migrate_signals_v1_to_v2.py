#!/usr/bin/env python3
"""关键信号 signals_json：扁平 v1 → 嵌套 v2（数组级 schema_version）迁移脚本。

来源：RFC《关键信号数据模型分层重构》§7（Phase 2）。
配套纯函数见 shared/schemas/signal_migration.py。本脚本只负责"把 DB/文件里的
signals_json 跑一遍 migrate_signal_document 并回写"，自身保持幂等、可干跑。

两种运行模式
------------
1) 文件模式（默认/测试用）：
     python migrate_signals_v1_to_v2.py --input dump.json --output migrated.json
   直接对 JSON 文件做转换，不碰数据库，便于 review diff。

2) 数据库模式（生产用，Phase 2 执行）：
     python migrate_signals_v1_to_v2.py --dsn "postgresql://hci_admin:...@postgres:5432/hci_troubleshoot"
   遍历 kbd_entry 表，对每条 signals_json 做迁移后 UPDATE 回写。

通用参数
--------
   --dry-run        只统计会变更的行数，不回写（强烈建议先跑一次）
   --only-changed   仅回写确实发生变化的行（幂等，重复执行安全）

依赖
----
   数据库模式需要 psycopg2（pip install psycopg2-binary）。文件模式无第三方依赖。

注意：本脚本改变 signals_json 的**列形态**（list → {schema_version, signals:[...]}），
必须在 RFC §7 Phase 2「所有读取方已切到 v2 嵌套段」之后执行；执行前请备份数据库。
"""
from __future__ import annotations

import argparse
import json

# 允许以脚本方式直接运行：将 backend 目录加入 sys.path 以导入 shared
import os
import sys
from typing import Any

_BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)

from shared.schemas.signal_migration import migrate_signal_document  # noqa: E402


def _needs_migration(raw: Any) -> bool:
    """判断一段 signals_json 是否仍需迁移（已是 v2 对象则跳过）。"""
    return not (isinstance(raw, dict) and raw.get("schema_version") == 2 and "signals" in raw)


def run_file_mode(input_path: str, output_path: str, dry_run: bool) -> None:
    with open(input_path, encoding="utf-8") as f:
        data = json.load(f)
    # 文件可能是整张表的 dump（[{id, signals_json}, ...]）或单段 signals_json
    if isinstance(data, list) and data and isinstance(data[0], dict) and "signals_json" in data[0]:
        rows = data
        changed = 0
        for row in rows:
            raw = row.get("signals_json")
            if _needs_migration(raw):
                row["signals_json"] = migrate_signal_document(raw)
                changed += 1
        if not dry_run:
            with open(output_path, "w", encoding="utf-8") as f:
                json.dump(rows, f, ensure_ascii=False, indent=2)
        print(f"[file] 处理 {len(rows)} 行，变更 {changed} 行；dry_run={dry_run}")
    else:
        migrated = migrate_signal_document(data)
        if not dry_run:
            with open(output_path, "w", encoding="utf-8") as f:
                json.dump(migrated, f, ensure_ascii=False, indent=2)
        print(f"[file] 单段迁移完成；dry_run={dry_run} -> {output_path}")


def run_db_mode(dsn: str, dry_run: bool, only_changed: bool) -> None:
    try:
        import psycopg2  # type: ignore
    except ImportError:
        print("缺少依赖 psycopg2：pip install psycopg2-binary", file=sys.stderr)
        sys.exit(2)

    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, signals_json FROM kbd_entry WHERE signals_json IS NOT NULL")
            rows = cur.fetchall()

        changed = 0
        updates: list[tuple[str, int]] = []
        for row_id, raw in rows:
            if not _needs_migration(raw):
                continue
            migrated = migrate_signal_document(raw)
            if only_changed and migrated == raw:
                continue
            updates.append((json.dumps(migrated, ensure_ascii=False), row_id))
            changed += 1

        print(f"[db] 扫描 {len(rows)} 行，将变更 {changed} 行；dry_run={dry_run}")
        if dry_run or not updates:
            return

        with conn.cursor() as cur:
            for payload, row_id in updates:
                cur.execute(
                    "UPDATE kbd_entry SET signals_json = %s::jsonb WHERE id = %s",
                    (payload, row_id),
                )
        conn.commit()
        print(f"[db] 已回写 {len(updates)} 行。")
    finally:
        conn.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="signals_json v1→v2 迁移（数组级 schema_version）")
    ap.add_argument("--input", help="文件模式：输入 JSON 路径")
    ap.add_argument("--output", help="文件模式：输出 JSON 路径")
    ap.add_argument("--dsn", help="数据库模式：PostgreSQL DSN")
    ap.add_argument("--dry-run", action="store_true", help="只统计变更，不回写")
    ap.add_argument("--only-changed", action="store_true", help="数据库模式：仅回写发生变化的行")
    args = ap.parse_args()

    if args.dsn:
        run_db_mode(args.dsn, args.dry_run, args.only_changed)
    elif args.input and args.output:
        run_file_mode(args.input, args.output, args.dry_run)
    else:
        ap.error("需提供 --dsn（数据库模式）或 --input/--output（文件模式）")


if __name__ == "__main__":
    main()
