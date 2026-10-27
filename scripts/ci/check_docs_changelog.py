#!/usr/bin/env python3
"""
变更历史节门禁 (docs changelog gate)

对应规范 §7.1「变更历史节（新建必须，存量守增量）」。此前 §7.1 明写
「本节的三行口径目前没有任何 CI 检查器强制」——V-036 点出的缺口：规范里
写着「必须」却没有任何门禁执行，等于没有这条规范。本脚本把它落成阻断级增量门禁。

第一性原理：变更历史节相对 `git log` 的唯一增量价值，是「关联事件文档」——
读者据此知道「这次改动当初为什么这么做」。而这条信息只有在改动当下才写得准，
git log 反推不出来。所以门禁的正当性来自：**实质改动了现行文档却不留这行，
轨迹就断了**。据此把 §7.1 的三行口径拆成可机器判定的硬约束（均以「本次 PR 增量」为准）：

  - 新建现行文档  : 必须建 `## 变更历史` 节，且至少一行真实日期数据行（§7.1 第 1 行）
  - 实质修改现行文档: 节内必须新增一行数据行；无节者须一并补建（§7.1 第 2、3 行）
  - 小修/纯删除    : 不触发（沿用 check_docs_rot 的 anti-noise 口径，见下）

对抗性审查（本门禁会在哪些条件下失效/被绕过，及明文边界）：
  1. 扫描范围只取 `docs/` 下的现行全量文档，**不含**仓库根 README/AGENTS 与模块
     README。理由：它们不与事件文档互联，其变更理由 git log 已完整捕获，再套一节
     只会产出与 git log 重复的第二份记录——与 §7.1 已对 `*/pitfalls/`、`_index.md`
     豁免同构。此边界必须写进 §7.1，不留给机器猜。
  2. 「实质」无法语义判定：机器只能抓「漏没漏一行」这一结构事实，判不了某行内容
     有无意义。有人用 `| 2020-01-01 | 略 | — |` 凑空行可骗过结构、骗不过评审。
     与规范 §九「否定窗口是启发式、控制手段在可见性上」同构：门禁的作用是把
     「默默忘了」变成「必须显式补一行」，内容真伪仍归人工评审。
  3. 「实质修改」的量化门限 = 节外新增/修改行数 ≥ `--min-lines`（默认 3）。低于此
     视为改错别字/单条链接/小措辞，不强制补行——否则改一个错字就要被迫动变更历史，
     与 check_docs_rot 「增量而非全量」防噪声的立场一致。
  4. 纯删除不触发：`list_changed_md_lines` 只登记新文件里新增/修改后的行号，删除行
     不产生新区间（继承 check_docs_rot 语义），因此只删旧内容的 PR 不会被误判。
  5. fail-closed：无法确定本次改动（缺 origin/main、路径 quoting 回归）时既不能
     静默降级为全量、也不能当作通过——与 check_docs_rot V-034 同源，沿用其两处
     相等断言守卫。

用法：
  python scripts/ci/check_docs_changelog.py            # 增量门禁（CI 用）
  python scripts/ci/check_docs_changelog.py --base <rev> --min-lines 3 --json
"""

from __future__ import annotations

import argparse
import json
import posixpath
import re
import subprocess
import sys
from pathlib import Path

# 与 §7.3 frontmatter / 腐化门禁共用同一份现行层口径与 git 采集实现，
# 避免「现行文档」定义在两个脚本里漂移（同 check_docs_rot 复用 check_docs_frontmatter）。
sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_docs_rot import (  # noqa: E402
    ROOT,
    is_current_layer,
    list_changed_md_files,
    list_changed_md_lines,
    list_content_changed_md_files,
)

# ─────────────────────────── 常量 ───────────────────────────

# 「实质修改」门限：节外新增/修改行数达到该值才要求追加变更历史行。
SUBSTANTIAL_LINE_THRESHOLD = 3

# 变更历史节标题：`## 变更历史`（允许 1~6 级标题与尾随空白）。
CHANGELOG_HEADING_RE = re.compile(r"^(#{1,6})\s*变更历史\s*$")
ANY_HEADING_RE = re.compile(r"^(#{1,6})\s")

# 数据行首列必须是真实日期（`2026-04-05`），据此天然排除表头「日期」行与 `|----|` 分隔行。
DATE_CELL_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ─────────────────────────── 纯判定函数（可单测）───────────────────────────


def in_changelog_scope(rel_path: str) -> bool:
    """判断仓库相对路径是否落在本门禁的扫描范围内。

    口径（对抗性审查第 1 点，须与 §7.1 明文一致）：
      - 必须属于现行全量文档层（复用 is_current_layer：非 archive、不含 events 段）；
      - 且必须位于 `docs/` 之下——有意排除仓库根 README/AGENTS 与模块 README；
      - 且不是 `*/pitfalls/` 下的条目文件、也不是 `_index.md` 注册表（§7.1 显式豁免：
        每个 V-xxx / PIT-xxx 条目本身就是带日期的变更记录，再套一节只会得到两份口径不一的记录）。
    """
    norm = rel_path.replace("\\", "/")
    if not is_current_layer(norm):
        return False
    if not norm.startswith("docs/"):
        return False
    if "/pitfalls/" in f"/{norm}":
        return False
    return posixpath.basename(norm) != "_index.md"


def find_changelog_section(lines: list[str]) -> tuple[int, int] | None:
    """返回变更历史节正文的行号区间 `[body_start, body_end]`（0 基、闭区间），无节返回 None。

    从标题行之后开始，直到下一个「同级或更高级」标题（或文件末尾）为止。
    """
    for idx, line in enumerate(lines):
        m = CHANGELOG_HEADING_RE.match(line)
        if not m:
            continue
        level = len(m.group(1))
        body_start = idx + 1
        body_end = len(lines) - 1
        for j in range(body_start, len(lines)):
            nm = ANY_HEADING_RE.match(lines[j])
            if nm and len(nm.group(1)) <= level:
                body_end = j - 1
                break
        return body_start, body_end
    return None


def is_data_row(line: str) -> bool:
    """判断某行是否为变更历史表格里的真实数据行（首列为 `YYYY-MM-DD`）。"""
    s = line.strip()
    if not s.startswith("|"):
        return False
    first = s.strip("|").split("|", 1)[0].strip()
    return bool(DATE_CELL_RE.match(first))


def section_has_data_row(lines: list[str], body_start: int, body_end: int) -> bool:
    """节正文内是否存在至少一行数据行。"""
    return any(is_data_row(lines[i]) for i in range(body_start, body_end + 1))


def evaluate_changelog(
    *,
    rel_path: str,
    post_text: str,
    changed_lines: set[int],
    is_new_file: bool,
    min_lines: int,
) -> tuple[bool, str]:
    """核心判定：给定改动后全文与本次新增/修改后的行号集合，返回 `(是否合规, 说明)`。

    `changed_lines` 为新文件里 1 基的行号集合（来自 list_changed_md_lines）。
    设计成纯函数、不触 git，以便单测覆盖各分支（对齐 test_check_docs_rot_semantics 的做法）。
    """
    lines = post_text.splitlines()
    sec = find_changelog_section(lines)
    has_row = sec is not None and section_has_data_row(lines, sec[0], sec[1])

    if is_new_file:
        # §7.1 第 1 行：新建现行文档必须建节并至少写一行「初版」。
        if sec is None:
            return False, "新建现行文档缺 `## 变更历史` 节"
        if not has_row:
            return False, "新建现行文档的变更历史节没有真实日期数据行"
        return True, ""

    # 修改场景：先算「节外」的新增/修改行数，判定是否达到「实质」门限。
    body_start, body_end = sec if sec else (-1, -2)  # 无节时用空区间
    outside = sum(1 for ln in changed_lines if 1 <= ln <= len(lines) and not (body_start <= ln - 1 <= body_end))
    if outside < min_lines:
        # 小修（改错别字/单条链接/措辞），不强制补变更历史行。
        return True, ""

    if sec is None:
        return False, f"实质修改（节外改动 {outside} 行）但无 `## 变更历史` 节，须补建并写≥1行"
    if not has_row:
        return False, f"实质修改（节外改动 {outside} 行）但变更历史节缺真实日期数据行，须补一行"

    # 需要节内至少有一行「本次新增/修改」的数据行，才算守住了增量轨迹。
    inside_new_row = any(
        body_start <= ln - 1 <= body_end and is_data_row(lines[ln - 1]) for ln in changed_lines if 1 <= ln <= len(lines)
    )
    if not inside_new_row:
        return False, f"实质修改（节外改动 {outside} 行）但变更历史节未新增数据行"
    return True, ""


def path_existed_in_base(rel_path: str, base: str) -> bool:
    """判断该路径在 base 版本是否已存在（用于区分「新建」与「修改」）。"""
    proc = subprocess.run(
        ["git", "cat-file", "-e", f"{base}:{rel_path}"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0


# ─────────────────────────── main ───────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="变更历史节门禁（§7.1）")
    parser.add_argument("--base", default="", help="比对基线（默认 merge-base(HEAD, origin/main)）")
    parser.add_argument(
        "--min-lines",
        type=int,
        default=SUBSTANTIAL_LINE_THRESHOLD,
        help="节外改动行数达到该值才算「实质修改」（默认 3）",
    )
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()

    try:
        if args.base:
            base = args.base
        else:
            base = subprocess.run(
                ["git", "merge-base", "HEAD", "origin/main"],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
        changed_rel = list_changed_md_files(base)
        lines_map = list_changed_md_lines(base)
        content_changed = list_content_changed_md_files(base)

        # fail-closed（对抗性审查第 5 点）：行号解析与 numstat 必须覆盖同一批
        # 「有内容变化」的文件，否则疑似路径 quoting 回归（与 check_docs_rot 同源守卫）。
        if set(lines_map) != content_changed:
            raise RuntimeError(
                f"行号解析文件集（{len(lines_map)}）与 numstat「有内容变化」文件集"
                f"（{len(content_changed)}）不一致（疑似路径 quoting 回归）"
            )
    except Exception as e:  # noqa: BLE001
        # 失败关闭：既不能静默降级为「无改动=通过」，也不能误放行。
        print(f"❌ 无法确定本次改动的文档，变更历史门禁失败关闭: {e}", file=sys.stderr)
        print("   常见原因：本地缺少 origin/main（请先 git fetch），或 git 路径解析回归。", file=sys.stderr)
        sys.exit(1)

    violations: list[dict] = []
    checked: list[str] = []
    for rel in sorted(set(changed_rel)):
        if not in_changelog_scope(rel):
            continue
        path = ROOT / rel
        if not path.is_file():
            # 删除文件已被 --diff-filter=d 排除；此处兜底跳过异常项。
            continue
        post_text = path.read_text(encoding="utf-8", errors="replace")
        is_new = not path_existed_in_base(rel, base)
        ok, msg = evaluate_changelog(
            rel_path=rel,
            post_text=post_text,
            changed_lines=set(lines_map.get(rel, set())),
            is_new_file=is_new,
            min_lines=args.min_lines,
        )
        checked.append(rel)
        if not ok:
            violations.append({"file": rel, "reason": msg, "is_new": is_new})

    if args.json:
        print(json.dumps({"checked": len(checked), "violations": violations}, ensure_ascii=False, indent=2))
    else:
        print(f"变更历史门禁：本次增量检查 {len(checked)} 篇现行 docs 文档")
        if violations:
            print(f"❌ {len(violations)} 篇实质改动未按 §7.1 追加变更历史行：", file=sys.stderr)
            for v in violations:
                mark = "新建" if v["is_new"] else "修改"
                print(f"   [{mark}] {v['file']} —— {v['reason']}", file=sys.stderr)
            print("   口径见 docs/文档管理规范.md §7.1（新建必须、存量守增量）。", file=sys.stderr)
            sys.exit(1)
        print("✅ 变更历史门禁通过")


if __name__ == "__main__":
    main()
