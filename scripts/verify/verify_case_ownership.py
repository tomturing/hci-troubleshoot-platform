#!/usr/bin/env python3
"""工单（case）归属完整性只读审计（SRC-L3，SRC-2026-5358 残余项）。

背景：平台无客户登录，工单归属键是 `"case".client_id`——由 api-gateway 依据服务端
签发的身份 Cookie（X-Client-ID）写入，客户凭 Cookie 回访自己的工单。因此"匿名工单"
并非可清理的孤儿数据，而是"无客户账号体系"模型的设计固有属性：Cookie 一旦丢失即
不可回访，但这**无法用 SQL 判定、更不得用 SQL 删除**（删除只会毁灭真实数据）。

本脚本不提供任何数据清理，只做**可观测审计 + 契约固化**，防止归属键被静默污染：

1. 纯函数 `classify_client_id(value)`：把 client_id 取值分类为归属形态合法（ok /
   anon_shape_ok）或异常（empty / placeholder / null）。异常形态意味着签发链或
   中间件被回退（如历史占位符 token 复活、匿名身份未签发），属真实缺陷。
2. 可选连库审计（`--database-url` 或环境变量 `DATABASE_URL`）：以只读 `SELECT`
   统计总数、异常归属、conversation 无对应 case、case.user_id 无对应 user
   （仅报告、不改数据）。`--strict` 时异常数 > 0 则非零退出，用于运维巡检 /
   发布前门禁。

归属形态依据（见 docs/security/identity-signature.md 与 staging 实测）：
- `client-<随机>`（历史/前端旧形态）、32-hex（当前网关 Cookie `uuid4().hex`）、
  `hci-sim-admin`（仿真系统身份）、`regression-client` 等测试种子——均为合法归属键。
- 空串 / 纯空白 / NULL / `client-session-placeholder-token`（SRC-L2 已删除的占位
  token）——异常，须告警。

用法：
    # 纯函数离线自检（CI 无库环境；等价单测见 tests）
    uv run python scripts/verify/verify_case_ownership.py --self-test
    # 连库只读审计
    uv run python scripts/verify/verify_case_ownership.py --database-url "$DATABASE_URL"
    # 严格模式（异常数>0 非零退出，作发布门禁）
    uv run python scripts/verify/verify_case_ownership.py --database-url "$DATABASE_URL" --strict
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import uuid
from typing import Literal

# 唯一调用链：本次审计执行的可追踪标识，输出到全部日志行
TRACE_ID = uuid.uuid4().hex

# SRC-L2 已删除的占位 token：任何工单以它为归属键都意味着桩鉴权/匿名签发链回退
PLACEHOLDER_LITERAL = "client-session-placeholder-token"

# 合法归属形态（存在即可回访，非孤儿）：client-<随机>、32-hex、hci-sim-admin、测试种子等。
# 正则仅用于识别"随机匿名 client_id"形态；未命中正则但非异常的取值仍视为合法归属键
# （如 hci-sim-admin 等系统身份），故分类只把明确的异常形态判为问题，保持保守。
RANDOM_CLIENT_ID_PATTERN = re.compile(r"^(client-[0-9a-z-]+|[0-9a-f]{32})$", re.IGNORECASE)

ClientClassification = Literal["ok", "anon_shape_ok", "empty", "placeholder", "null"]


def log(level: str, message: str) -> None:
    """带调用链的结构化日志行。"""
    print(f"[{TRACE_ID}] {level}  {message}")


def classify_client_id(value: object) -> ClientClassification:
    """把工单归属键 client_id 分类为合法或异常形态。

    返回：
    - "null"        ：值为 None（归属键缺失，签发链断裂）。
    - "empty"       ：空串或纯空白（归属键被清空）。
    - "placeholder" ：历史占位符 token（SRC-L2 已删除，复发即桩鉴权回退）。
    - "anon_shape_ok"：匹配随机匿名 client_id 形态（client-<随机> / 32-hex）。
    - "ok"          ：其它非空取值（如 hci-sim-admin 等系统/测试身份），归属键存在即合法。
    """

    if value is None:
        return "null"
    if not isinstance(value, str):
        # 非字符串（如整型误写）不应出现在 varchar 归属键，保守判异常，按 empty 归一告警
        return "empty"
    text = value.strip()
    if not text:
        return "empty"
    if text == PLACEHOLDER_LITERAL:
        return "placeholder"
    if RANDOM_CLIENT_ID_PATTERN.fullmatch(text):
        return "anon_shape_ok"
    return "ok"


ABNORMAL_CLASSIFICATIONS = ("empty", "placeholder", "null")


def _normalize_database_url(url: str) -> str:
    """把 SQLAlchemy asyncpg scheme 归一为 psycopg 可识别的 postgresql:// scheme。"""
    return url.replace("postgresql+asyncpg://", "postgresql://")


def audit_database(database_url: str, strict: bool) -> int:
    """连库执行只读归属审计，返回异常归属总数（用于 --strict 退出码判定）。

    仅执行 SELECT，绝不写库。conversation 归属经 case_id 二跳到 case.client_id；
    case.user_id 孤儿仅在 "user" 表存在时统计。
    """

    try:
        import psycopg  # 同步驱动，审计脚本足够
    except ImportError:
        log("ERROR", "未安装 psycopg，无法连库审计；请改用 --self-test 或安装依赖：uv pip install psycopg[binary]")
        return 2

    total = 0
    abnormal_rows: dict[str, int] = {"empty": 0, "placeholder": 0, "null": 0}
    abnormal_client_ids: dict[str, int] = {}
    conversation_orphans = 0
    user_orphans = 0
    user_table_exists = False

    url = _normalize_database_url(database_url)
    # 只读事务：防止审计脚本意外写库（第一性原理：门禁工具必须无副作用）
    with psycopg.connect(url) as conn, conn.cursor() as cur:
        try:
            cur.execute("SET TRANSACTION READ ONLY")
        except psycopg.Error:
            # 某些版本/配置不支持事务级只读，降级为不设防但仍只 SELECT
            log("INFO", "READ ONLY 事务设置失败，继续以只 SELECT 方式审计")

        cur.execute('SELECT client_id FROM "case"')
        for (client_id,) in cur.fetchall():
            total += 1
            kind = classify_client_id(client_id)
            if kind in ABNORMAL_CLASSIFICATIONS:
                abnormal_rows[kind] += 1
            else:
                key = "<anon-random>" if kind == "anon_shape_ok" else str(client_id)
                abnormal_client_ids[key] = abnormal_client_ids.get(key, 0) + 1

        # conversation 无对应 case（会话孤儿）
        cur.execute(
            'SELECT count(*) FROM conversation c LEFT JOIN "case" k ON c.case_id = k.case_id WHERE k.case_id IS NULL'
        )
        conversation_orphans = cur.fetchone()[0]

        # case.user_id 无对应 "user"（仅当 user 表存在）
        cur.execute("SELECT to_regclass('public.\"user\"')")
        user_table_exists = cur.fetchone()[0] is not None
        if user_table_exists:
            cur.execute(
                'SELECT count(*) FROM "case" k LEFT JOIN "user" u ON k.user_id = u.user_id WHERE u.user_id IS NULL'
            )
            user_orphans = cur.fetchone()[0]
        conn.rollback()  # 显式回滚，确保零写入

    abnormal_total = sum(abnormal_rows.values())
    log("INFO", f"工单归属审计 trace_id={TRACE_ID}")
    log("INFO", f"case 总数：{total}")
    log(
        "INFO",
        f"异常归属：empty={abnormal_rows['empty']} placeholder={abnormal_rows['placeholder']} null={abnormal_rows['null']}",
    )
    log("INFO", f"conversation 无对应 case：{conversation_orphans}")
    if user_table_exists:
        log("INFO", f"case.user_id 无对应 user：{user_orphans}")
    else:
        log("INFO", '未找到 "user" 表，跳过 case.user_id 孤儿统计')

    # 归属形态分布（帮助运维识别数据健康度）
    log("INFO", "合法归属键分布（Top）：")
    for key, cnt in sorted(abnormal_client_ids.items(), key=lambda kv: -kv[1]):
        log("INFO", f"    {key}: {cnt}")

    if abnormal_total:
        log("WARN", f"存在 {abnormal_total} 条异常归属工单（签发链可能回退，须排查）")
    else:
        log("OK", "无异常归属工单（empty/placeholder/null 均为 0）")

    if strict and (abnormal_total or conversation_orphans or user_orphans):
        log("FAIL", "--strict：检测到异常归属或孤儿，非零退出")
        return 1
    return 0


def self_test() -> int:
    """离线纯函数自检：无需数据库即可验证分类契约，返回失败用例数。"""

    cases: list[tuple[object, ClientClassification]] = [
        (None, "null"),
        ("", "empty"),
        ("   ", "empty"),
        (PLACEHOLDER_LITERAL, "placeholder"),
        ("client-mpayv9r9-2cox3cx", "anon_shape_ok"),
        ("a1b2c3d4e5f60718293a4b5c6d7e8f90", "anon_shape_ok"),  # 32-hex
        ("hci-sim-admin", "ok"),
        ("regression-client", "ok"),
        ("client-conv-001", "anon_shape_ok"),
        ("client-test-001", "anon_shape_ok"),
    ]
    failures = 0
    for value, expected in cases:
        got = classify_client_id(value)
        status = "OK" if got == expected else "FAIL"
        if got != expected:
            failures += 1
        log(status, f"classify_client_id({value!r}) = {got} (期望 {expected})")
    if failures:
        log("FAIL", f"纯函数自检失败 {failures} 例")
    else:
        log("PASS", f"纯函数自检通过（{len(cases)} 例）")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description="工单（case）归属完整性只读审计（SRC-L3）")
    parser.add_argument(
        "--database-url", default=os.getenv("DATABASE_URL", ""), help="连库审计的数据库 URL（或 DATABASE_URL 环境变量）"
    )
    parser.add_argument("--strict", action="store_true", help="异常归属/孤儿数>0 时非零退出（发布门禁）")
    parser.add_argument("--self-test", action="store_true", help="仅执行离线纯函数分类自检，不连库")
    args = parser.parse_args()

    if args.self_test:
        return 1 if self_test() else 0

    if not args.database_url:
        log("ERROR", "未提供 --database-url / DATABASE_URL，无法连库审计；如需离线自检请用 --self-test")
        return 2
    return audit_database(args.database_url, strict=args.strict)


if __name__ == "__main__":
    sys.exit(main())
