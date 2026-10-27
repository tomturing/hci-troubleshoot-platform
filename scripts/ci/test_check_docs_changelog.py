#!/usr/bin/env python3
"""check_docs_changelog.py 的回归测试。

函数名一律 ASCII snake_case（本仓库 ruff `select` 含 N 系列，中文函数名会报 N802，
语义写进 docstring——同 V-037 记录）。测试只驱动纯判定函数 with 合成输入，不触 git。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from check_docs_changelog import (  # noqa: E402
    evaluate_changelog,
    find_changelog_section,
    in_changelog_scope,
    is_data_row,
)

# 一份「合规」样例全文：正文若干行 + 变更历史节含表头/分隔/两行数据。
SAMPLE_WITH_LOG = """# 标题

正文第一行
正文第二行
正文第三行

## 变更历史

| 日期 | 版本 | 变更内容 | 关联事件文档 |
|------|------|---------|------------|
| 2026-04-05 | v2.1 | 新增 X 模块 | [方案](events/a.md) |
| 2026-03-19 | v1.0 | 初版 | — |
"""


def _lines(text: str) -> list[str]:
    return text.splitlines()


def test_new_file_without_section_is_violation():
    """新建现行文档没有变更历史节 → 不合规。"""
    ok, msg = evaluate_changelog(
        rel_path="docs/solution/x.md",
        post_text="# 标题\n\n正文\n",
        changed_lines={1, 2, 3},
        is_new_file=True,
        min_lines=3,
    )
    assert ok is False
    assert "缺" in msg


def test_new_file_with_data_row_passes():
    """新建文档含变更历史节且有真实日期数据行 → 合规。"""
    total = len(_lines(SAMPLE_WITH_LOG))
    ok, _ = evaluate_changelog(
        rel_path="docs/solution/x.md",
        post_text=SAMPLE_WITH_LOG,
        changed_lines=set(range(1, total + 1)),
        is_new_file=True,
        min_lines=3,
    )
    assert ok is True


def test_new_file_section_without_date_row_is_violation():
    """新建文档有节但只有表头/分隔、无数据行 → 不合规。"""
    text = "# 标题\n\n## 变更历史\n\n| 日期 | 版本 |\n|------|------|\n"
    ok, msg = evaluate_changelog(
        rel_path="docs/solution/x.md",
        post_text=text,
        changed_lines={1, 2, 3, 4, 5},
        is_new_file=True,
        min_lines=3,
    )
    assert ok is False
    assert "数据行" in msg


def test_modified_small_change_is_exempt():
    """修改但节外改动低于门限（改错别字）→ 免，不要求补行。"""
    ok, _ = evaluate_changelog(
        rel_path="docs/solution/x.md",
        post_text=SAMPLE_WITH_LOG,
        changed_lines={3},
        is_new_file=False,
        min_lines=3,
    )
    assert ok is True


def test_modified_substantial_with_new_row_passes():
    """实质修改且节内新增了数据行 → 合规。"""
    # 在正文新增 4 行（行 3-6 落在节外），并在表内新增一行数据（末行）。
    text = (
        "# 标题\n\n"
        "新1\n新2\n新3\n新4\n\n"
        "## 变更历史\n\n"
        "| 日期 | 版本 |\n|------|------|\n"
        "| 2026-05-01 | v2.2 | 加内容 | — |\n"
    )
    changed = {3, 4, 5, 6, 12}
    ok, msg = evaluate_changelog(
        rel_path="docs/solution/x.md",
        post_text=text,
        changed_lines=changed,
        is_new_file=False,
        min_lines=3,
    )
    assert ok is True, msg


def test_modified_substantial_without_new_row_is_violation():
    """实质修改但变更历史节没有新增数据行 → 不合规。"""
    text = (
        "# 标题\n\n"
        "新1\n新2\n新3\n新4\n\n"
        "## 变更历史\n\n"
        "| 日期 | 版本 | 变更内容 | 关联事件文档 |\n|------|------|---------|------------|\n"
        "| 2026-04-05 | v2.1 | 旧行 | — |\n"
    )
    # 只在节外改了 4 行，节内一行没动。
    ok, msg = evaluate_changelog(
        rel_path="docs/solution/x.md",
        post_text=text,
        changed_lines={3, 4, 5, 6},
        is_new_file=False,
        min_lines=3,
    )
    assert ok is False
    assert "未新增数据行" in msg


def test_modified_substantial_no_section_requires_backfill():
    """实质修改一篇尚无变更历史节的存量文档 → 要求补建。"""
    text = "# 标题\n\n新1\n新2\n新3\n新4\n正文\n"
    ok, msg = evaluate_changelog(
        rel_path="docs/solution/legacy.md",
        post_text=text,
        changed_lines={3, 4, 5, 6},
        is_new_file=False,
        min_lines=3,
    )
    assert ok is False
    assert "补建" in msg


def test_pure_deletion_not_penalized():
    """纯删除（本次无新增/修改行）→ 节外改动 0 行，不触发门禁。"""
    ok, _ = evaluate_changelog(
        rel_path="docs/solution/x.md",
        post_text="# 标题\n\n瘦身后正文\n",
        changed_lines=set(),
        is_new_file=False,
        min_lines=3,
    )
    assert ok is True


def test_modified_section_present_but_no_data_row_distinct_message():
    """存量文档有节但缺日期数据行（实测仓内 7 篇）→ 报「缺数据行」，不得误报为「无节」。"""
    text = "# 标题\n\n新1\n新2\n新3\n新4\n\n## 变更历史\n\n| 日期 | 版本 |\n|------|------|\n"
    ok, msg = evaluate_changelog(
        rel_path="docs/solution/x.md",
        post_text=text,
        changed_lines={3, 4, 5, 6},
        is_new_file=False,
        min_lines=3,
    )
    assert ok is False
    assert "缺真实日期数据行" in msg
    assert "无 `## 变更历史` 节" not in msg


def test_scope_current_docs_only():
    """扫描范围只含 docs/ 现行文档，排除事件/归档/根 README-AGENTS/pitfalls/_index。"""
    assert in_changelog_scope("docs/solution/x.md") is True
    assert in_changelog_scope("docs/solution/events/2026-01-01-a.md") is False
    assert in_changelog_scope("docs/archive/old.md") is False
    assert in_changelog_scope("README.md") is False
    assert in_changelog_scope("AGENTS.md") is False
    assert in_changelog_scope("backend/service/README.md") is False
    assert in_changelog_scope("docs/verify/pitfalls/python.md") is False
    assert in_changelog_scope("docs/verify/pitfalls/_index.md") is False
    assert in_changelog_scope("docs/verify/_index.md") is False


def test_find_section_stops_at_same_level_heading():
    """变更历史节正文区间在下一个同级标题处截止。"""
    text = "## 变更历史\n\n| a |\n| 2026-01-01 | x |\n\n## 附录\n\n无关行\n| 2027-01-01 | y |\n"
    start, end = find_changelog_section(_lines(text))
    # 行 0 标题；正文 1..4（含 `## 附录` 前的空行）；行 5 为 `## 附录`（同级），截止于其前一行。
    assert start == 1
    assert end == 4
    # 附录里的日期行不应落入区间。
    assert "| 2027-01-01" not in "\n".join(_lines(text)[start : end + 1])


def test_is_data_row_rejects_header_and_separator():
    """数据行判定只认真实日期，排除表头与分隔行。"""
    assert is_data_row("| 2026-04-05 | v2.1 | 新增 | — |") is True
    assert is_data_row("| 日期 | 版本 | 变更内容 |") is False
    assert is_data_row("|------|------|---------|") is False
    assert is_data_row("| 2026-4-5 | v2 | 非零填充 | — |") is False
    assert is_data_row("普通文本行") is False


def test_run_gate_on_repo_smoke():
    """冒烟：脚本可作为模块导入且关键常量到位（真实 git 增量由 CI 与本 PR 自证）。"""
    from check_docs_changelog import SUBSTANTIAL_LINE_THRESHOLD

    assert SUBSTANTIAL_LINE_THRESHOLD == 3
