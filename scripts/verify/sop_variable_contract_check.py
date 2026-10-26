#!/usr/bin/env python3
"""SOP 变量声明契约巡检。

对任意环境（staging/prod）的 sop_document.variable_schema 跑一次契约校验，
用于：
  * CI / 发布前的数据门禁（退出码非 0 即阻断）
  * 线上巡检（发现被 SQL 直改坏的声明）
  * 变量包漂移检测（applied_packs 版本落后 / 变量被改回去）

用法::

    DATABASE_URL=postgresql://user:pwd@host:5432/hci_troubleshoot \\
        python scripts/verify/sop_variable_contract_check.py

    # 只看已发布
    ... --status published

    # 输出 JSON 供流水线消费
    ... --json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "backend"))

from shared.models.sop_variable import validate_variable_schema  # noqa: E402


async def _load_sops(database_url: str, status: str | None) -> list[dict]:
    try:
        import asyncpg
    except ImportError:  # pragma: no cover - 运维环境可能未装 asyncpg
        print("需要 asyncpg：pip install asyncpg", file=sys.stderr)
        raise SystemExit(2) from None

    conn = await asyncpg.connect(database_url)
    try:
        sql = """
            SELECT id, title, status, variable_schema
            FROM sop_document
            WHERE variable_schema IS NOT NULL
              AND jsonb_typeof(variable_schema) = 'array'
        """
        params: list[object] = []
        if status:
            sql += " AND status = $1"
            params.append(status)
        sql += " ORDER BY id"
        rows = await conn.fetch(sql, *params)
        return [
            {
                "id": row["id"],
                "title": row["title"],
                "status": row["status"],
                "schema": json.loads(row["variable_schema"]),
            }
            for row in rows
        ]
    finally:
        await conn.close()


def _check_one(sop: dict) -> list[dict]:
    issues: list[dict] = []
    schema = sop["schema"]

    for mode in ("save", "publish"):
        report = validate_variable_schema(schema, mode=mode)
        for issue in report.errors:
            issues.append(
                {
                    "sop_id": sop["id"],
                    "sop_title": sop["title"],
                    "mode": mode,
                    "variable": issue.variable,
                    "code": issue.code,
                    "message": issue.message,
                }
            )

    # 变量池键名必须是已声明变量（防御"写入野键"回归）
    declared = {item.get("name") for item in schema if isinstance(item, dict)}
    for item in schema:
        if not isinstance(item, dict):
            continue
        for dep in item.get("depends_on") or []:
            if dep not in declared:
                issues.append(
                    {
                        "sop_id": sop["id"],
                        "sop_title": sop["title"],
                        "mode": "dependency",
                        "variable": item.get("name"),
                        "code": "SOP_VAR_DEP_NOT_DECLARED",
                        "message": f"依赖 '{dep}' 未在 schema 中声明",
                    }
                )
    return issues


async def main() -> int:
    parser = argparse.ArgumentParser(description="SOP 变量声明契约巡检")
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL"))
    parser.add_argument("--status", default=None, help="仅检查指定状态，如 published")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    args = parser.parse_args()

    if not args.database_url:
        print("缺少 --database-url 或 DATABASE_URL", file=sys.stderr)
        return 2

    sops = await _load_sops(args.database_url, args.status)
    all_issues: list[dict] = []
    for sop in sops:
        all_issues.extend(_check_one(sop))

    if args.json:
        print(json.dumps({"checked": len(sops), "issues": all_issues}, ensure_ascii=False, indent=2))
    else:
        print(f"检查 SOP {len(sops)} 份，发现违规 {len(all_issues)} 条")
        for issue in all_issues:
            print(
                f"  [SOP#{issue['sop_id']} {issue['sop_title']}] "
                f"{issue['variable']} ({issue['mode']}): {issue['code']} - {issue['message']}"
            )

    return 1 if all_issues else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
