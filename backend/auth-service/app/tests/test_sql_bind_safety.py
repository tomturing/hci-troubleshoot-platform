"""原生 SQL 绑定参数写法安全契约。

背景：`repository.get_roles` 曾写成
``SELECT roles FROM "user" WHERE user_id = :uid::uuid``，
SQLAlchemy ``text()`` 的绑定参数解析与 PostgreSQL ``::`` 类型转换语法冲突，
经 asyncpg 下发后出现 ``syntax error at or near ":"``，线上管理员登录直接 500。

单测用 mock session，**无法覆盖真实 SQL 解析**（构建期 import 冒烟同样覆盖不到），
因此这里用静态检查兜底：凡是 ``text()`` 里的 SQL 都不允许出现 ``::``。
"""

from __future__ import annotations

import re
from pathlib import Path

# app/tests/ → app/
_APP_ROOT = Path(__file__).resolve().parents[1]

# 按引号类型分别匹配，允许 SQL 内嵌另一种引号（如 'FROM "user"'）
_TEXT_CALL_RE = re.compile(r"text\(\s*(?:'([^']*)'|\"([^\"]*)\")")

# 只认 SQL 片段：包含以下关键字之一，避免把注释里的示例误判为真实 SQL
_SQL_KEYWORDS = ("SELECT", "INSERT", "UPDATE", "DELETE", "WITH")


def sql_snippets() -> list[tuple[str, str]]:
    """收集生产代码里所有 text("...") 的 SQL 片段及其所在文件（跳过本目录的测试）。"""
    found: list[tuple[str, str]] = []
    for path in sorted(_APP_ROOT.rglob("*.py")):
        parts = path.parts
        if "__pycache__" in parts or path.name.startswith("test_"):
            continue
        content = path.read_text(encoding="utf-8")
        for match in _TEXT_CALL_RE.finditer(content):
            snippet = (match.group(1) or match.group(2) or "").strip()
            if any(word in snippet.upper() for word in _SQL_KEYWORDS):
                found.append((str(path.relative_to(_APP_ROOT)), snippet))
    return found


def test_text_sql_has_no_postgres_cast_operator() -> None:
    """text() 内禁止 PostgreSQL `::` 转换：请用 CAST(x AS type) 代替。"""
    offenders = [(f, sql) for f, sql in sql_snippets() if "::" in sql]
    assert not offenders, (
        "text() 内出现 PostgreSQL `::` 类型转换，会与 :param 绑定参数解析冲突；"
        f"请改用 CAST(x AS type)。违规项：{offenders}"
    )


def test_contract_sees_enough_samples() -> None:
    """防止扫描正则失效导致契约变成空集合而通过。"""
    assert len(sql_snippets()) >= 3, "扫描到的 text() SQL 过少，检查正则或目录结构是否变化"
