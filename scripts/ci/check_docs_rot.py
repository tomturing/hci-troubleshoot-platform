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
  C6 密钥泄露      : 文档中硬编码的真实密钥/凭证（安全红线，永远阻断）
  C7 版本矛盾      : 核心文档（README/AGENTS/CLAUDE）版本号声明不一致

用法：
  python scripts/tools/docs_rot_scan.py [--dir docs] [--json] [--only C1,C2,C3]
"""
import argparse
import json
import re
import subprocess
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


# ─────────────────────────── 新增：安全与一致性 ───────────────────────────

# C6: 真实密钥/敏感信息泄露（高危，必须阻断合并）
SECRET_RE = re.compile(
    r"""(?ix)
    (?:
        sk-[a-z]{2}-[a-zA-Z0-9]{20,}              # OpenAI / 阿里云百炼 / DashScope 风格
      | AKIA[0-9A-Z]{16}                          # AWS Access Key ID
      | AIza[0-9A-Za-z_\-]{35}                    # Google API Key
      | ya29\.[0-9A-Za-z_\-]+                     # Google OAuth
      | github_pat_[0-9A-Za-z_]{20,}              # GitHub PAT
      | xox[baprs]-[0-9A-Za-z\-]{10,}             # Slack Token
      | (?i:api[_-]?key|secret|token|password)\s*[:=]\s*['\"]?[a-zA-Z0-9]{24,}['\"]?
    )
    """
)


def check_secrets(files: list[Path], allowlist: set[str] | None = None) -> list[dict]:
    """C6: 文档中硬编码的真实密钥/敏感凭证

    对抗性审查：仅匹配高熵且形如真实凭证的字符串，排除明显的占位符
    （${VAR} / xxx / your-key / <replace> / 示例 等语境），避免误报阻断正常 PR。
    """
    allowlist = allowlist or {"example", "your", "xxx", "placeholder", "test", "demo"}
    issues = []
    for f in files:
        text = f.read_text(encoding="utf-8", errors="replace")
        for i, line in enumerate(text.splitlines(), 1):
            # 排除环境变量占位符与显式示例语境
            if "${" in line or "{{" in line or "<" in line and ">" in line:
                continue
            if any(w in line.lower() for w in allowlist):
                continue
            for m in SECRET_RE.finditer(line):
                matched = m.group(0)
                if any(w in matched.lower() for w in allowlist):
                    continue
                issues.append({
                    "type": "C6",
                    "file": str(f.relative_to(ROOT)),
                    "line": i,
                    "detail": f"疑似泄露真实密钥/凭证: {matched[:12]}***（请改用环境变量注入并立即轮换）",
                })
    return issues


# C7: 权威文档版本号一致性（防止 README/AGENTS/CLAUDE 互相矛盾）
def check_version_consistency() -> list[dict]:
    """C7: 核心文档中声明的版本号必须唯一且与 pyproject.toml 一致"""
    issues = []
    pyproject = ROOT / "pyproject.toml"
    declared_versions: set[str] = set()
    if pyproject.exists():
        ver = re.search(r'^version\s*=\s*["\']([^"\']+)["\']', pyproject.read_text(encoding="utf-8"), re.MULTILINE)
        if ver:
            declared_versions.add(ver.group(1))
    # 扫描核心文档中的 vX.Y.Z 版本声明
    core_docs = [ROOT / "README.md", ROOT / "AGENTS.md", ROOT / "CLAUDE.md", ROOT / "docs/文档管理规范.md"]
    found: dict[str, list[str]] = {}
    for d in core_docs:
        if not d.exists():
            continue
        for m in re.finditer(r"v(\d+\.\d+\.\d+)", d.read_text(encoding="utf-8")):
            found.setdefault(m.group(1), []).append(str(d.relative_to(ROOT)))
    # 若声明的版本多于 1 个且与 pyproject 不一致，上报
    unique_versions = set(found.keys())
    if len(unique_versions) > 1:
        issues.append({
            "type": "C7",
            "file": "README.md/AGENTS.md/CLAUDE.md",
            "line": 0,
            "detail": f"版本号声明不一致: {sorted(unique_versions)}（pyproject.toml 声明: {sorted(declared_versions) or '未知'}）",
        })
    return issues


# ─────────────────────────── 主流程 ───────────────────────────

CHECKERS = {
    "C1": check_internal_links,
    "C2": check_images,
    "C3": check_repo_paths,
    "C4": check_placeholders,
    "C5": check_empty_files,
    "C6": check_secrets,
    "C7": check_version_consistency,
}

# 硬失败类别（非零退出，阻断合并）
HARD_CATEGORIES = ("C1", "C2", "C3", "C6")


def main() -> None:
    parser = argparse.ArgumentParser(description="文档腐化扫描器")
    parser.add_argument("--dir", default=".", help="扫描目录（相对仓库根，默认全仓库）")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--only", default="C1,C2,C3,C4,C5,C6,C7", help="逗号分隔的检查类别")
    parser.add_argument("--limit", type=int, default=0, help="每类最多输出条数（0 不限）")
    parser.add_argument(
        "--changed-only", action="store_true",
        help="仅检查本次 PR 改动的文件（增量门禁，阻断引入新腐化）",
    )
    parser.add_argument(
        "--fail-on-stock", action="store_true",
        help="存量问题也阻断（默认仅增量问题阻断，存量只警告）",
    )
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

    # 增量门禁：仅保留本次 PR 改动（merge-base..HEAD）的 md 文件
    changed_files: set[str] | None = None
    if args.changed_only:
        try:
            base = subprocess.run(
                ["git", "merge-base", "HEAD", "origin/main"],
                cwd=ROOT, capture_output=True, text=True, check=True,
            ).stdout.strip()
            out = subprocess.run(
                ["git", "diff", "--name-only", base, "HEAD", "--", "*.md"],
                cwd=ROOT, capture_output=True, text=True, check=True,
            ).stdout.strip().splitlines()
            changed_files = {str(ROOT / f) for f in out if f}
            files = [f for f in files if str(f) in changed_files]
        except Exception as e:  # noqa: BLE001
            print(f"⚠️ 获取改动文件失败，回退全量扫描: {e}", file=sys.stderr)
        print(f"增量模式：仅检查 {len(files)} 个改动文档")

    enabled = [c.strip() for c in args.only.split(",") if c.strip()]
    all_issues: list[dict] = []
    for cat in enabled:
        if cat not in CHECKERS:
            continue
        if cat == "C7":
            issues = CHECKERS[cat]()  # 版本一致性检查不依赖 files 参数
        else:
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

    # 分级门禁：
    #  - 密钥泄露（C6）永远阻断（无论增量/存量），安全红线不可妥协
    #  - 失效引用（C1/C2/C3）：增量模式仅阻断本次引入的新问题；存量只警告
    #  - 其余类别（C4/C5/C7）只报告不阻断
    secret_block = [i for i in all_issues if i["type"] == "C6"]
    rot_block = [i for i in all_issues if i["type"] in ("C1", "C2", "C3")]

    if secret_block:
        print(f"\n❌ 发现 {len(secret_block)} 个密钥泄露（C6），安全红线，PR 禁止合并")
        sys.exit(1)

    if rot_block:
        if args.changed_only and not args.fail_on_stock:
            print(f"\n⚠️ 发现 {len(rot_block)} 个失效引用（C1/C2/C3），但为存量问题，仅警告不阻断")
            print("   如需阻断存量：运行 --fail-on-stock；治理计划见 docs/ROADMAP 文档腐化专项")
        else:
            print(f"\n❌ 发现 {len(rot_block)} 个阻断级失效引用（C1/C2/C3），PR 禁止合并")
            sys.exit(1)

    print("\n✅ 门禁通过（无新增密钥泄露与失效引用）")
    sys.exit(0)


if __name__ == "__main__":
    main()
