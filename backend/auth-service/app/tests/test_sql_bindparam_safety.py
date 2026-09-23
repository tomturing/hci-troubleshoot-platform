"""原生 SQL 绑定参数安全检查。

背景：text('... WHERE user_id = :uid::uuid') 在 SQLAlchemy + asyncpg 下会被错误解析，
下发到 Postgres 报 syntax error at or near ":"（线上登录 500 的根因）。

这类缺陷的特性是三层都测不到：
1. 单测用 mock session，不触达真实 SQL；
2. 镜像构建期 import 冒烟只验证模块可导入；
3. CI 单测在根虚拟环境里跑，没有真实库。

所以这里做静态兜底：任何 text() 里的 SQL 都不允许出现 ::，统一改用 CAST(:param AS type)。
"""

from __future__ import annotations

import re
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]

# 匹配 text("...") / text('...') 中的 SQL 字面量
TEXT_SQL_RE = re.compile(r"text\(\s*(['\"])(.{1,500}?)\1", re.S)


def _sql_snippets() -> list[tuple[str, str]]:
    """返回 (文件名#序号, SQL 片段)。"""
    out: list[tuple[str, str]] = []
    for py in sorted(APP_ROOT.rglob("*.py")):
        parts = py.parts
        if "tests" in parts or "__pycache__" in parts:
            continue
        content = py.read_text(encoding="utf-8")
        for idx, sql in enumerate(TEXT_SQL_RE.findall(content)):
            out.append((f"{py.relative_to(APP_ROOT)}#{idx}", sql))
    return out


def test_text_sql_has_no_double_colon_cast() -> None:
    """text() 中的 SQL 禁止 :: 强转，必须用 CAST(:param AS type)。"""
    offenders = [(name, sql.strip()) for name, sql in _sql_snippets() if "::" in sql and "\\:" not in sql]
    assert not offenders, (
        "以下原生 SQL 使用了 :: 强转，在 text() 下会与绑定参数解析冲突"
        "（改用 CAST(:param AS type)）：\n" + "\n".join(f"- {name}: {snippet}" for name, snippet in offenders)
    )


def test_guard_is_effective() -> None:
    """防御惯性：确保扫描确实抓到了 SQL，否则上面的断言会变成空跑。"""
    assert _sql_snippets(), "未扫描到任何 text() SQL，正则或路径可能已失效"
