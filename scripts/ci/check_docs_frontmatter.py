#!/usr/bin/env python3
"""docs/ 现行全量文档的 frontmatter 规范检查

契约来源：docs/文档管理规范.md
  - §7.3    必填字段 status / category / audience / last_updated / owner
  - §7.3.1  status 只有 active / archived（描述文档生命周期，不描述功能状态）
  - §7.3.2  audience 用受控词表，逗号+空格分隔多值，all 独占

检查范围：仅「现行全量文档」。两类被排除，理由是它们由别的规则管：
  - events/  事件文档遵循 §8，记录当时决策，不按现行契约维护
  - archive/ 归档件只读，frontmatter 冻结在搬入时刻

存在理由（第一性原理）：frontmatter 的唯一用途是让读者（含 Agent）按
status / category / audience 机械过滤文档。一旦某篇缺失字段或写了词表外的
值，过滤就会静默漏掉它——而「静默漏掉」比报错危险得多：读者以为已看全，
实际少看了几篇。所以这里不设「建议级」通道，不合规即阻断。

用法：
  python3 scripts/ci/check_docs_frontmatter.py            # PR 模式：仅检查改动文件
  python3 scripts/ci/check_docs_frontmatter.py --full     # 全量：docs/ 下全部现行全量文档

环境变量：
  BASE_SHA / HEAD_SHA   PR 模式下 git diff 范围（CI 注入）；缺省则比对工作区与 HEAD
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

# ─────────────────────────── 契约常量 ───────────────────────────

DOCS_BASE = Path("docs")

REQUIRED_FIELDS = ("status", "category", "audience", "last_updated", "owner")

# §7.3.1：只有两个合法值
STATUS_VALUES = {"active", "archived"}

# §7.3 模板：category 六选一，取值由文档所在目录决定
CATEGORY_VALUES = {"requirement", "solution", "task", "deploy", "verify", "meta"}

# §7.3.2：受控词表。新增角色必须先改规范，再改这里——不允许「先放过再补规范」
AUDIENCE_VALUES = {
    "agent",
    "developer",
    "operator",
    "product",
    "architect",
    "qa",
    "tester",
    "sre",
    "security",
    "reviewer",
    "all",
}

# 目录名命中即豁免（按路径分段匹配，避免误伤 solution/agent/events 之外的同名子串）
EVENT_DIR_NAME = "events"
ARCHIVE_TOP_DIR = "archive"

FRONTMATTER_RE = re.compile(r"^---\n(?P<body>.*?)\n---\n", re.S)
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# 逗号后必须跟一个空格（§7.3.2 规则 1），且不允许多个空格
AUDIENCE_SEP_RE = re.compile(r",(?! )|,\s{2,}")


# ─────────────────────────── 路径分类 ───────────────────────────


def docs_relative_parts(rel: str | Path) -> tuple[str, ...]:
    """把 docs/ 相对路径拆成路径分段（统一分隔符）。"""
    return Path(rel).parts


def is_current_doc(rel: str | Path) -> bool:
    """该路径是否属于「现行全量文档」（需要遵守 §7.3 的那一类）。"""
    parts = docs_relative_parts(rel)
    if not parts or not parts[-1].endswith(".md"):
        return False
    if parts[0] == ARCHIVE_TOP_DIR:
        return False
    return EVENT_DIR_NAME not in parts[:-1]


# ─────────────────────────── 单文件检查 ───────────────────────────


def parse_frontmatter(text: str) -> dict[str, str] | None:
    """解析首个 --- 块。缺失或未闭合返回 None。

    只认文件最开头的那一块：正文里出现的 --- 是水平分割线，不是元数据。
    """
    if text.startswith("\ufeff"):
        text = text[1:]
    match = FRONTMATTER_RE.match(text)
    if match is None:
        return None
    fields: dict[str, str] = {}
    for line in match.group("body").splitlines():
        if ":" not in line:
            # 缩进的列表续行（如 related_prs 的多行值）不参与字段解析
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        # 允许 `key: value` 后跟行内注释（模板里就是这么写的）
        value = value.split("#", 1)[0].strip()
        if key:
            fields[key] = value
    return fields


def check_frontmatter(rel: str | Path, text: str) -> list[str]:
    """检查单篇文档的 frontmatter，返回阻断项列表（空表示通过）。"""
    fields = parse_frontmatter(text)
    if fields is None:
        return [
            f"❌ docs/{rel}\n   缺少 frontmatter（文档第 1 行必须是 --- 块）\n   需补齐：{' / '.join(REQUIRED_FIELDS)}"
        ]

    problems: list[str] = []

    missing = [f for f in REQUIRED_FIELDS if not fields.get(f)]
    if missing:
        problems.append(f"   缺少必填字段：{' / '.join(missing)}")

    status = fields.get("status", "")
    if status and status not in STATUS_VALUES:
        problems.append(
            f"   status='{status}' 非法，只允许 {' / '.join(sorted(STATUS_VALUES))}"
            f"（功能是否落地写在正文，不进 status，见 §7.3.1）"
        )

    category = fields.get("category", "")
    if category and category not in CATEGORY_VALUES:
        problems.append(f"   category='{category}' 非法，必须是 {' / '.join(sorted(CATEGORY_VALUES))} 之一")

    audience = fields.get("audience", "")
    if audience:
        if AUDIENCE_SEP_RE.search(audience):
            problems.append(f"   audience='{audience}' 分隔符不规范，应写成 'developer, qa'（逗号+单个空格）")
        roles = [r.strip() for r in audience.split(",")]
        unknown = [r for r in roles if r not in AUDIENCE_VALUES]
        if unknown:
            problems.append(
                f"   audience 含词表外角色：{' / '.join(unknown)}"
                f"（受控词表见 §7.3.2，历史遗留的 engineer 视为 developer）"
            )
        if len(roles) > 1 and "all" in roles:
            problems.append("   audience 的 'all' 必须独占，不能与具体角色并列")

    last_updated = fields.get("last_updated", "")
    if last_updated and not DATE_RE.match(last_updated):
        problems.append(f"   last_updated='{last_updated}' 非法，必须是 YYYY-MM-DD")

    if not problems:
        return []
    return [f"❌ docs/{rel}\n" + "\n".join(problems)]


# ─────────────────────────── 文件枚举 ───────────────────────────


def iter_full_scan() -> list[Path]:
    """全量模式：docs/ 下所有现行全量文档（docs/ 相对路径）。"""
    result: list[Path] = []
    if not DOCS_BASE.is_dir():
        return result
    for path in sorted(DOCS_BASE.rglob("*.md")):
        rel = path.relative_to(DOCS_BASE)
        if is_current_doc(rel):
            result.append(rel)
    return result


def iter_changed(base: str | None, head: str | None) -> list[Path]:
    """PR 模式：改动/新增/改名的现行全量文档（docs/ 相对路径）。

    用 `-z` 而不是 `--name-only` 的逐行解析：本仓文档大量使用中文文件名，
    行式解析一旦被 core.quotepath 转成八进制转义就会静默漏检（V-034 同源缺陷）。
    """
    cmd = [
        "git",
        "-c",
        "core.quotepath=false",
        "diff",
        "--name-only",
        "-z",
        "--diff-filter=d",  # 排除删除，其余（含 rename 目标）都要检查
    ]
    if base and head:
        cmd += [base, head]
    else:
        cmd += ["HEAD"]
    cmd += ["--", "docs/*.md", "docs/**/*.md"]

    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(f"git diff 失败：{proc.stderr.decode('utf-8', 'replace')}")

    changed: list[Path] = []
    for raw in proc.stdout.decode("utf-8").split("\0"):
        if not raw or not raw.startswith("docs/"):
            continue
        rel = Path(raw).relative_to(DOCS_BASE)
        if is_current_doc(rel):
            changed.append(rel)
    return changed


# ─────────────────────────── 入口 ───────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="docs/ frontmatter 规范检查（§7.3）")
    parser.add_argument("--full", action="store_true", help="全量检查 docs/（默认仅检查改动文件）")
    args = parser.parse_args()

    if args.full:
        targets = iter_full_scan()
        scope = "全量"
    else:
        targets = iter_changed(os.environ.get("BASE_SHA"), os.environ.get("HEAD_SHA"))
        scope = "改动"

    findings: list[str] = []
    for rel in targets:
        path = DOCS_BASE / rel
        if not path.is_file():
            continue
        # errors='replace'：frontmatter 是纯 ASCII，正文里的编码问题不应让门禁崩掉
        findings.extend(check_frontmatter(rel, path.read_text(encoding="utf-8", errors="replace")))

    if not findings:
        print(f"✅ frontmatter 规范检查通过（{scope} {len(targets)} 篇）")
        sys.exit(0)

    print("=" * 60)
    print(f"📋 frontmatter 规范检查 — {len(findings)} 篇不合规（{scope}扫描 {len(targets)} 篇）")
    print("=" * 60)
    for finding in findings:
        print(finding)
        print()
    print("─" * 60)
    print("契约：docs/文档管理规范.md §7.3 / §7.3.1 / §7.3.2")
    print("为何阻断：缺少字段或用词表外的值时，按 status/category/audience 过滤")
    print("会静默漏掉该文档——读者以为已看全，实际少看了几篇。")
    print("─" * 60)
    sys.exit(1)


if __name__ == "__main__":
    main()
