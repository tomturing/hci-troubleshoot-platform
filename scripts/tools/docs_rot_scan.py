#!/usr/bin/env python3
"""
文档腐化扫描器 (docs rot scanner)

第一性原理：文档的根基使命是"准确反映系统当前状态，可被可靠执行/引用"。
本脚本从底层事实出发，检测五类腐化：

  C1 失效内部链接  : [text](相对路径) 指向的文件不存在
  C2 缺失图片资源  : ![alt](path) 引用的图片文件不存在
  C3 失效仓库路径  : 反引号/代码块中形如 backend/、scripts/、deploy/ 的仓库内路径不存在
  C4 占位符腐化    : TODO / TBD / 待补充 / 待完善 / FIXME / XXX 等未完成标记
  C5 空/极短文档   : 内容过短或仅剩占位符，无法提供有效信息

用法：
  python scripts/tools/docs_rot_scan.py [--dir docs] [--json] [--only C1,C2,C3]
"""
import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

# ─────────────────────────── 常量 ───────────────────────────

ROOT = Path(__file__).resolve().parents[2]

# C3: 文档中反引号包裹的仓库内路径前缀（存在性可验证）
REPO_PATH_PREFIXES = (
    "backend/", "frontend/", "scripts/", "deploy/", "database/",
    "docs/", "tests/", "data-pipeline/", "hci_sim/", "evaluation/",
    "terminal_bridge/", ".github/", "skills/", ".agents/",
)
REPO_PATH_RE = re.compile(
    r"`((?:"
    + "|".join(re.escape(p) for p in REPO_PATH_PREFIXES)
    + r")[^`\s]+)`"
)

# C4: 占位符
PLACEHOLDER_RE = re.compile(
    r"(TODO|TBD|FIXME|XXX|待补充|待完善|待确认|待实现|占位|待定|此处填写|待更新|coming soon|not implemented|placeholder)",
    re.IGNORECASE,
)

# 代码围栏
FENCE_RE = re.compile(r"^```", re.MULTILINE)


# ─────────────────────────── 工具函数 ───────────────────────────

def is_fenced(line: str, fences: list[tuple[int, int]]) -> bool:
    """判断行号是否在代码围栏内"""
    for start, end in fences:
        if start <= line <= end:
            return True
    return False


def compute_fences(text: str) -> list[tuple[int, int]]:
    """计算代码围栏行范围（奇数行开、偶数行关）"""
    lines = text.splitlines()
    fences = []
    starts = []
    for i, line in enumerate(lines, 1):
        if FENCE_RE.match(line):
            if starts:
                fences.append((starts.pop(), i))
            else:
                starts.append(i)
    return fences


def resolve_target(base_file: Path, target: str) -> Path | None:
    """解析 markdown 链接目标为仓库内绝对路径；外部链接/锚点/协议返回 None"""
    if not target:
        return None
    # 去除锚点
    target = target.split("#")[0]
    if not target:
        return None
    # 协议链接（http/mailto 等）或根路径 /api 等
    parsed = urlparse(target)
    if parsed.scheme or target.startswith("/"):
        return None
    target = unquote(target)
    if target.endswith("/"):
        target = target.rstrip("/") + "/index.md"
    candidate = (base_file.parent / target).resolve()
    # 防止逃逸到仓库外
    if not str(candidate).startswith(str(ROOT)):
        return None
    return candidate


def check_internal_links(files: list[Path]) -> list[dict]:
    """C1: 内部链接指向不存在的文件"""
    issues = []
    link_re = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
    for f in files:
        text = f.read_text(encoding="utf-8", errors="replace")
        fences = compute_fences(text)
        for i, line in enumerate(text.splitlines(), 1):
            if is_fenced(i, fences):
                continue
            for m in link_re.finditer(line):
                raw = m.group(1)
                # 跳过代码示例中的链接（带空格/多余符号的不解析）
                target = resolve_target(f, raw)
                if target is None:
                    continue
                if not target.exists():
                    issues.append({
                        "type": "C1",
                        "file": str(f.relative_to(ROOT)),
                        "line": i,
                        "detail": f"链接目标不存在: [{raw}] -> {target.relative_to(ROOT)}",
                    })
    return issues


def check_images(files: list[Path]) -> list[dict]:
    """C2: 图片引用缺失"""
    issues = []
    img_re = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
    for f in files:
        text = f.read_text(encoding="utf-8", errors="replace")
        fences = compute_fences(text)
        for i, line in enumerate(text.splitlines(), 1):
            if is_fenced(i, fences):
                continue
            for m in img_re.finditer(line):
                raw = m.group(1)
                target = resolve_target(f, raw)
                if target is None:
                    continue
                if not target.exists():
                    issues.append({
                        "type": "C2",
                        "file": str(f.relative_to(ROOT)),
                        "line": i,
                        "detail": f"图片缺失: [{raw}] -> {target.relative_to(ROOT)}",
                    })
    return issues


def check_repo_paths(files: list[Path]) -> list[dict]:
    """C3: 反引号中的仓库内路径不存在"""
    issues = []
    for f in files:
        text = f.read_text(encoding="utf-8", errors="replace")
        fences = compute_fences(text)
        for i, line in enumerate(text.splitlines(), 1):
            if is_fenced(i, fences):
                continue
            for m in REPO_PATH_RE.finditer(line):
                p = m.group(1)
                # 去除可能的行号前缀、锚点、通配符
                clean = p.split("#")[0].split(":")[0].strip("*/\"'()")
                if any(ch in clean for ch in "*?<>|"):
                    continue
                if clean.endswith((".md", ".py", ".sh", ".yaml", ".yml", ".json",
                                   ".ts", ".vue", ".sql", ".toml", ".conf", ".go",
                                   ".dockerfile", ".env", ".txt", ".js", ".ini")):
                    # 仅对明确带扩展名的路径做存在性校验（避免误报目录名/示例）
                    if not (ROOT / clean).exists() and not (ROOT / clean).is_dir():
                        # 忽略带版本号/变量插值的路径
                        if "$" in clean or "{{" in clean:
                            continue
                        issues.append({
                            "type": "C3",
                            "file": str(f.relative_to(ROOT)),
                            "line": i,
                            "detail": f"仓库路径不存在: `{clean}`",
                        })
    return issues


def check_placeholders(files: list[Path]) -> list[dict]:
    """C4: 占位符/未完成标记

    对抗性审查要点：项目本身大量使用"占位符"作为业务术语
    （如模板占位符 {{var}}、图片占位符 ![img:N]），这些是正常语义，
    不属于腐化。仅当标记出现在"文档自身声明未完成"的语境时才上报。
    """
    issues = []
    # 业务术语语境下的"占位符"不视为腐化（白名单）
    business_terms = ("{{", "}}", "![img:", "模板占位符", "图片占位符", "变量占位符",
                      "环境变量占位符", "placeholder-token", "usage_template",
                      "image_placeholder", "content_md", "占位符集合", "占位符格式",
                      "占位符校验", "占位符转义", "占位符从", "占位符和候选",
                      "占位符在 schema", "占位符 requires", "占位符 src")
    strong_markers = ("TODO", "TBD", "FIXME", "待补充", "待完善", "待确认", "待实现",
                      "待更新", "此处填写", "XXX", "未实现", "尚未实现", "未完成", "尚未完成")
    for f in files:
        text = f.read_text(encoding="utf-8", errors="replace")
        # 忽略代码围栏内的 TODO（可能是示例代码）
        fences = compute_fences(text)
        for i, line in enumerate(text.splitlines(), 1):
            if is_fenced(i, fences):
                continue
            # 仅当出现强未完成标记时上报；纯业务术语（模板/图片占位符）不算腐化
            for m in PLACEHOLDER_RE.finditer(line):
                if m.group(0).upper() not in ("TODO", "TBD", "FIXME", "XXX"):
                    # 中文占位符需同时出现在强标记中才上报
                    if not any(sm in line for sm in strong_markers):
                        continue
                # "xxx" 用作示例命名（env:xxx、xxx-service、xxx 命令）不算腐化
                if m.group(0).upper() == "XXX" and re.search(r"(env|service|命令|model|plugin|route|view|api|格式|参数|字段|端口|host|path|接口|module|package|tool|skill):?xxx|xxx[-_]", line, re.IGNORECASE):
                    continue
                issues.append({
                    "type": "C4",
                    "file": str(f.relative_to(ROOT)),
                    "line": i,
                    "detail": f"占位符: {m.group(0)} | {line.strip()[:80]}",
                })
    return issues


def check_empty_files(files: list[Path]) -> list[dict]:
    """C5: 空/极短文档"""
    issues = []
    for f in files:
        text = f.read_text(encoding="utf-8", errors="replace")
        visible = len([l for l in text.splitlines() if l.strip()])
        if visible == 0:
            issues.append({
                "type": "C5",
                "file": str(f.relative_to(ROOT)),
                "line": 0,
                "detail": "空文件",
            })
        elif visible <= 2:
            issues.append({
                "type": "C5",
                "file": str(f.relative_to(ROOT)),
                "line": 0,
                "detail": f"极短文档（{visible} 行有效内容）: {text.strip()[:80]}",
            })
    return issues


# ─────────────────────────── 主流程 ───────────────────────────

CHECKERS = {
    "C1": check_internal_links,
    "C2": check_images,
    "C3": check_repo_paths,
    "C4": check_placeholders,
    "C5": check_empty_files,
}


def main() -> None:
    parser = argparse.ArgumentParser(description="文档腐化扫描器")
    parser.add_argument("--dir", default="docs", help="扫描目录（相对仓库根）")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--only", default="C1,C2,C3,C4,C5", help="逗号分隔的检查类别")
    parser.add_argument("--limit", type=int, default=0, help="每类最多输出条数（0 不限）")
    args = parser.parse_args()

    scan_root = ROOT / args.dir
    if not scan_root.is_dir():
        print(f"目录不存在: {scan_root}", file=sys.stderr)
        sys.exit(1)

    files = sorted(
        p for p in scan_root.rglob("*.md")
        if ".git" not in p.parts
        and "node_modules" not in p.parts
        and ".venv" not in p.parts
        and "__pycache__" not in p.parts
        and ".pytest_cache" not in p.parts
    )
    print(f"扫描 {len(files)} 个文档，目录 {scan_root.relative_to(ROOT)}")

    enabled = [c.strip() for c in args.only.split(",") if c.strip()]
    all_issues: list[dict] = []
    for cat in enabled:
        if cat not in CHECKERS:
            continue
        issues = CHECKERS[cat](files)
        print(f"\n[{cat}] 发现 {len(issues)} 个问题")
        shown = issues[:args.limit] if args.limit else issues
        for it in shown:
            print(f"  {it['file']}:{it['line']}  {it['detail']}")
        all_issues.extend(issues)

    print("\n" + "=" * 60)
    print("汇总：")
    for cat in enabled:
        n = len([i for i in all_issues if i["type"] == cat])
        print(f"  {cat}: {n}")
    print(f"  总计: {len(all_issues)}")

    if args.json:
        print("\nJSON:")
        print(json.dumps(all_issues, ensure_ascii=False, indent=1))

    # 退出码：有 C1/C2/C3 硬性问题时非零
    hard = [i for i in all_issues if i["type"] in ("C1", "C2", "C3")]
    sys.exit(1 if hard else 0)


if __name__ == "__main__":
    main()
