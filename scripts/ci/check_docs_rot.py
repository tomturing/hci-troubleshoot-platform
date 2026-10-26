#!/usr/bin/env python3
"""
文档腐化扫描器 (docs rot scanner)

第一性原理：文档的根基使命是"准确反映系统当前状态，可被可靠执行/引用"。
本脚本从底层事实出发，检测五类腐化：

  C1 失效内部链接  : [text](相对路径) 指向的文件不存在（指向已存在目录的目录链接视为有效）
  C2 缺失图片资源  : ![alt](path) 引用的图片文件不存在
  C3 失效仓库路径  : 反引号/代码块中形如 backend/、scripts/、deploy/ 的仓库内路径不存在
                     （同行紧邻处声明「不存在/已删除」等事实的引用不阻断，但显式列出）
                     被 .gitignore 刻意排除的路径（secrets.env 等）不阻断，但显式列出
                     行内代码包裹的 markdown 语法示例不参与 C1/C2 判定
                     frontmatter status=archived 的文档整体豁免 C1/C2/C3（见 §7.3.1，C6 不豁免）
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
    "backend/",
    "frontend/",
    "scripts/",
    "deploy/",
    "database/",
    "docs/",
    "tests/",
    "data-pipeline/",
    "hci_sim/",
    "evaluation/",
    "terminal_bridge/",
    ".github/",
    "skills/",
    ".agents/",
)
REPO_PATH_RE = re.compile(r"`((?:" + "|".join(re.escape(p) for p in REPO_PATH_PREFIXES) + r")[^`\s]+)`")

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
    return any(start <= line <= end for start, end in fences)


def inline_code_spans(line: str) -> list[tuple[int, int]]:
    """给出行内代码（反引号包裹）的字符区间。

    第一性原理：反引号内的内容是被当作**字面文本**呈现的，不是超链接。
    文档里大量出现「示范禁止写法」的 markdown 语法（如表格中写
    `` `![截图](URL)` `` 来表示“重写后不得出现这种形式”）。把这
    类语法示例当失效链接上报，会迫使作者去改一个本身正确的句子。
    """
    return [(m.start(), m.end()) for m in re.finditer(r"`[^`]*`", line)]


def in_spans(pos: int, spans: list[tuple[int, int]]) -> bool:
    """判断字符位置是否落在任一行内代码区间内"""
    return any(start <= pos < end for start, end in spans)


# C3 豁免语境：文档在**陈述某路径不存在或已被删除**时，该引用是正确的记录方式。
# 回归背景：docs/solution/数据库设计.md 变更历史有一行明文写
#   「`data-pipeline/kbd/ingest.py` 与 `scripts/sop/import.py` 均为不存在的路径」，
# 这正是本门禁要防止的“把正确的事实断言当缺陷报错”。若为了让门禁变绿而去
# “修”这句，等于逼文档说谎。
# 对抗性审查：只扫“同行任意位置”会给出过大的射程——作者在任意行添一
# 个「已删除」就能让任意坏路径逃过检查。因此要求职词必须**紧邻**该路径
# （前 30 字符或后 60 字符内），与人类阅读时的修饰关系一致。
NEGATION_WINDOW_BEFORE = 30
NEGATION_WINDOW_AFTER = 60
NEGATION_WORDS = (
    "不存在",
    "已删除",
    "删除了",
    "已移除",
    "移除了",
    "已废弃",
    "废弃了",
    "不再存在",
    "曾用名",
    "历史路径",
    "原路径",
    "旧路径",
    "已被重命名",
    "git 历史",
)


def negation_context(line: str, start: int, end: int) -> str | None:
    """若路径邻近位置出现否定语义词，返回命中的词；否则返回 None。"""
    window = line[max(0, start - NEGATION_WINDOW_BEFORE) : start] + line[end : end + NEGATION_WINDOW_AFTER]
    for w in NEGATION_WORDS:
        if w in window:
            return w
    return None


# gitignore 判定缓存：仓库相对路径 -> 命中的规则串（未命中为 None）
_IGNORE_CACHE: dict[str, str | None] = {}


def gitignore_rule(rel_path: str) -> str | None:
    """返回该仓库相对路径命中的 .gitignore 规则（`源:行号:模式`），未命中返回 None。

    第一性原理：C3 的正当性来自「读者能按文档找到该文件」。但仓库里有一批
    **刻意不入库**的文件——`deploy/env/secrets.env`（.gitignore 显式排除的密钥文件）、
    `data-pipeline/kbd/.env`、运行期生成的 `logs/`。文档指挥运维「配置 secrets.env」
    是正确指令，要求它在检出里存在等于逼文档说谎。
    回归背景：docs/deploy/部署指南.md:938 因该缺陷长期挂红。

    对抗性审查：豁免判据必须来自作者之外。此处由 .gitignore 决定，作者若想借它
    逃逸就得改 .gitignore——那是会被单独评审的改动；且豁免逐条打印命中的规则，
    「整目录忽略」这类宽规则人工一眼可辨。非 git 仓库（单测临时目录）返回 None，
    保守不豁免。
    """
    if rel_path in _IGNORE_CACHE:
        return _IGNORE_CACHE[rel_path]
    try:
        proc = subprocess.run(
            ["git", "-C", str(ROOT), "check-ignore", "-v", "--no-index", rel_path],
            capture_output=True,
            text=True,
        )
    except OSError:
        rule = None
    else:
        rule = proc.stdout.strip() or None
    _IGNORE_CACHE[rel_path] = rule
    return rule


# §7.3 frontmatter 契约与腐化检测必须共用同一份解析实现，否则两处对 status
# 的口径会漂移（一个认为 archived、另一个还在按 active 要求引用完整）。
sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_docs_frontmatter import parse_frontmatter  # noqa: E402


def is_current_layer(rel_file: str) -> bool:
    """判断仓库相对路径是否属于「现行全量文档」层。

    口径：不是 `docs/archive/` 下的文档，且路径中不含 `events/` 目录段。
    这有意包含仓库根的 README.md / AGENTS.md 与 `docs/` 外的现行 md——它们
    同样是读者会照着行动的现行文档，没有理由豁免。

    第一性原理：门禁为什么要分级？因为不同层的**读者行为**不同：
      - 现行文档：读者会照着动手，一条失效引用就是真实故障，
        因此可归零、也应归零；
      - 事件文档（`*/events/`）：读的是「当时为什么这么决策」，把史料引用
        「修成现行」等于伪造历史（规范 §8.3）；
      - 归档（`docs/archive/`）：只读，§12.3 不得被现行文档依赖。
    所以只有现行层能升为阻断级。

    对抗性审查：
      - 路径分隔符在 Windows 上可能是反斜杠，先归一化；
      - `docs/archive/` 只看前缀，而 `events` 只看**目录段**（前后补 `/` 再
        包含），所以 `docs/solution/events.md` 这类同名文件不会被误剥。
    """
    norm = rel_file.replace("\\", "/")
    if norm.startswith("docs/archive/"):
        return False
    return "/events/" not in f"/{norm}"


def select_blocking(
    rot_block: list[dict],
    changed_lines: dict[str, set[int]] | None,
    *,
    changed_only: bool,
    fail_on_stock: bool,
    current_blocking: bool,
) -> tuple[list[dict], list[dict], int]:
    """从失效引用（C1/C2/C3）清单中挑出应当阻断的条目。

    返回 `(阻断清单, 其中因现行层而阻断的清单, 不阻断的存量数)`，后两者是
    前者的子集/补集，供 main 打印时分别措辞，判定结果只有一处真源。

    第一性原理：门禁的正当性只来自「读者会照文档行动」。
      - 现行全量文档层已归零，该层任何一条都是新引入的故障 -> 一律阻断；
      - 事件层/归档层是史料，为变绿而改写它们等于伪造历史（规范 §8.3）
        -> 只按「本次改动行」判增量，存量仅警告。

    对抗性审查：
      - 关闭 `--current-blocking` 时行为必须与旧版完全一致（不得让新开关
        在默认路径上偷偷收紧或放宽）；
      - `changed_lines is None` 在现行 `--changed-only` 路径上不可达（取不到
        diff 已经 fail-closed 退出），但作为纯函数必须对这个入参有定义：此时
        没有依据区分「本次引入」，非现行层不阻断，现行层命中与来源无关仍阻断；
      - 用 `file:line:detail` 三元组去重，避免同一命中既算改动行又算现行层
        而被计两次。
    """
    current_hits = [i for i in rot_block if current_blocking and is_current_layer(i["file"])]
    if changed_only and not fail_on_stock:
        introduced = [
            i for i in rot_block if changed_lines is not None and i["line"] in changed_lines.get(i["file"], set())
        ]
        merged = {f"{i['file']}:{i['line']}:{i['detail']}" for i in introduced}
        extra = [i for i in current_hits if f"{i['file']}:{i['line']}:{i['detail']}" not in merged]
        blocking = introduced + extra
        return blocking, extra, len(rot_block) - len(blocking)
    return list(rot_block), list(current_hits), 0


def split_archived(files: list[Path]) -> tuple[list[Path], list[Path]]:
    """按 frontmatter status 拆为（需检查, archived 豁免）。

    第一性原理：检查引用有效性的正当性来自「读者会依文档行动」。
    §7.3.1 强制 archived 文档首屏写明「已废弃 + 可核实依据」，读者被告知不得据其
    行动；而它引用的代码正是随废弃一并删除的。此时仍要求路径存在，等于
    逼一份已声明作废的文档说谎。

    对抗性审查：该豁免可被「随手标 archived」逃逸。三重限制：
    1. §7.3.1 本身要求 archived 必须给出可复核证据（提交号/实库行数）；
    2. status 字段受 check_docs_frontmatter 阻断级门禁约束，不能乱写；
    3. 豁免文档必须逐条显式列出，不做静默跳过。
    密钥检查（C6）不受此豁免影响，安全红线不区分文档状态。
    """
    active, archived = [], []
    for f in files:
        try:
            fm = parse_frontmatter(f.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            fm = None
        if fm and fm.get("status") == "archived":
            archived.append(f)
        else:
            active.append(f)
    return active, archived


def archived_waiver(f: Path, cat: str) -> dict:
    """为一篇被豁免的 archived 文档生成一条显式可审计的记录。"""
    return {
        "type": cat,
        "file": str(f.relative_to(ROOT)),
        "line": 1,
        "detail": "整篇引用有效性检查豁免",
        "waived": True,
        "waive_reason": "frontmatter status=archived（§7.3.1 原地归档，读者不得据其行动）",
    }


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
        # 目录链接在 GitHub 上会渲染该目录的文件列表页，本身就是可用信源，
        # 不强求目录里必须有 index.md。
        # 回归背景：docs/README.md 用 [task/](task/) 形式列目录导航，全部被
        # 误报为「链接目标不存在: -> docs/task/index.md」，导致门禁失真。
        # 只放宽到「目录确实存在」，目录本身不存在时仍然报错，不降低检出能力。
        dir_cand = (base_file.parent / target.rstrip("/")).resolve()
        if dir_cand.is_dir() and str(dir_cand).startswith(str(ROOT)):
            return dir_cand
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
    files, archived = split_archived(files)
    for f in archived:
        issues.append(archived_waiver(f, "C1"))
    for f in files:
        text = f.read_text(encoding="utf-8", errors="replace")
        fences = compute_fences(text)
        for i, line in enumerate(text.splitlines(), 1):
            if is_fenced(i, fences):
                continue
            spans = inline_code_spans(line)
            for m in link_re.finditer(line):
                if in_spans(m.start(), spans):
                    continue  # 行内代码里的语法示例不是真链接
                raw = m.group(1)
                target = resolve_target(f, raw)
                if target is None:
                    continue
                if not target.exists():
                    issues.append(
                        {
                            "type": "C1",
                            "file": str(f.relative_to(ROOT)),
                            "line": i,
                            "detail": f"链接目标不存在: [{raw}] -> {target.relative_to(ROOT)}",
                        }
                    )
    return issues


def check_images(files: list[Path]) -> list[dict]:
    """C2: 图片引用缺失"""
    issues = []
    img_re = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
    files, archived = split_archived(files)
    for f in archived:
        issues.append(archived_waiver(f, "C2"))
    for f in files:
        text = f.read_text(encoding="utf-8", errors="replace")
        fences = compute_fences(text)
        for i, line in enumerate(text.splitlines(), 1):
            if is_fenced(i, fences):
                continue
            spans = inline_code_spans(line)
            for m in img_re.finditer(line):
                if in_spans(m.start(), spans):
                    continue  # 行内代码里的语法示例不是真图片引用
                raw = m.group(1)
                target = resolve_target(f, raw)
                if target is None:
                    continue
                if not target.exists():
                    issues.append(
                        {
                            "type": "C2",
                            "file": str(f.relative_to(ROOT)),
                            "line": i,
                            "detail": f"图片缺失: [{raw}] -> {target.relative_to(ROOT)}",
                        }
                    )
    return issues


def check_repo_paths(files: list[Path]) -> list[dict]:
    """C3: 反引号中的仓库内路径不存在"""
    issues = []
    files, archived = split_archived(files)
    for f in archived:
        issues.append(archived_waiver(f, "C3"))
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
                # 仅对明确带扩展名的路径做存在性校验（避免误报目录名/示例）
                if (
                    clean.endswith(
                        (
                            ".md",
                            ".py",
                            ".sh",
                            ".yaml",
                            ".yml",
                            ".json",
                            ".ts",
                            ".vue",
                            ".sql",
                            ".toml",
                            ".conf",
                            ".go",
                            ".dockerfile",
                            ".env",
                            ".txt",
                            ".js",
                            ".ini",
                        )
                    )
                    and not (ROOT / clean).exists()
                    and not (ROOT / clean).is_dir()
                ):
                    # 忽略带版本号/变量插值的路径
                    if "$" in clean or "{{" in clean:
                        continue
                    ignored = gitignore_rule(clean)
                    if ignored:
                        # 该路径被 .gitignore 刻意排除，本来就不该出现在检出里
                        issues.append(
                            {
                                "type": "C3",
                                "file": str(f.relative_to(ROOT)),
                                "line": i,
                                "detail": f"仓库路径不存在: `{clean}`",
                                "waived": True,
                                "waive_reason": f"被 .gitignore 刻意排除（{ignored}）",
                            }
                        )
                        continue
                    neg = negation_context(line, m.start(), m.end())
                    if neg:
                        # 文档正在陈述该路径不存在/已删除——这是正确记录，不阻断但显式打印
                        issues.append(
                            {
                                "type": "C3",
                                "file": str(f.relative_to(ROOT)),
                                "line": i,
                                "detail": f"仓库路径不存在: `{clean}`",
                                "waived": True,
                                "waive_reason": f"邻近否定语义词「{neg}」，判定为删除事实记录",
                            }
                        )
                        continue
                    issues.append(
                        {
                            "type": "C3",
                            "file": str(f.relative_to(ROOT)),
                            "line": i,
                            "detail": f"仓库路径不存在: `{clean}`",
                        }
                    )
    return issues


def check_placeholders(files: list[Path]) -> list[dict]:
    """C4: 占位符/未完成标记

    对抗性审查要点：项目本身大量使用"占位符"作为业务术语
    （如模板占位符 {{var}}、图片占位符 ![img:N]），这些是正常语义，
    不属于腐化。仅当标记出现在"文档自身声明未完成"的语境时才上报。
    """
    issues = []
    # 白名单已收敛为下方的强未完成标记：仅当标记本身属于“文档声明未完成”
    # 语义时才上报，纯业务术语（模板占位符 {{var}}、图片占位符 ![img:N]）不算腐化。
    strong_markers = (
        "TODO",
        "TBD",
        "FIXME",
        "待补充",
        "待完善",
        "待确认",
        "待实现",
        "待更新",
        "此处填写",
        "XXX",
        "未实现",
        "尚未实现",
        "未完成",
        "尚未完成",
    )
    for f in files:
        text = f.read_text(encoding="utf-8", errors="replace")
        # 忽略代码围栏内的 TODO（可能是示例代码）
        fences = compute_fences(text)
        for i, line in enumerate(text.splitlines(), 1):
            if is_fenced(i, fences):
                continue
            # 仅当出现强未完成标记时上报；纯业务术语（模板/图片占位符）不算腐化
            for m in PLACEHOLDER_RE.finditer(line):
                # 中文占位符需同时出现在强标记中才上报
                if m.group(0).upper() not in ("TODO", "TBD", "FIXME", "XXX") and not any(
                    sm in line for sm in strong_markers
                ):
                    continue
                # "xxx" 用作示例命名（env:xxx、xxx-service、xxx 命令）不算腐化
                if m.group(0).upper() == "XXX" and re.search(
                    r"(env|service|命令|model|plugin|route|view|api|格式|参数|字段|端口|host|path|接口|module|package|tool|skill):?xxx|xxx[-_]",
                    line,
                    re.IGNORECASE,
                ):
                    continue
                issues.append(
                    {
                        "type": "C4",
                        "file": str(f.relative_to(ROOT)),
                        "line": i,
                        "detail": f"占位符: {m.group(0)} | {line.strip()[:80]}",
                    }
                )
    return issues


def check_empty_files(files: list[Path]) -> list[dict]:
    """C5: 空/极短文档"""
    issues = []
    for f in files:
        text = f.read_text(encoding="utf-8", errors="replace")
        visible = len([line for line in text.splitlines() if line.strip()])
        if visible == 0:
            issues.append(
                {
                    "type": "C5",
                    "file": str(f.relative_to(ROOT)),
                    "line": 0,
                    "detail": "空文件",
                }
            )
        elif visible <= 2:
            issues.append(
                {
                    "type": "C5",
                    "file": str(f.relative_to(ROOT)),
                    "line": 0,
                    "detail": f"极短文档（{visible} 行有效内容）: {text.strip()[:80]}",
                }
            )
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
                issues.append(
                    {
                        "type": "C6",
                        "file": str(f.relative_to(ROOT)),
                        "line": i,
                        "detail": f"疑似泄露真实密钥/凭证: {matched[:12]}***（请改用环境变量注入并立即轮换）",
                    }
                )
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
        issues.append(
            {
                "type": "C7",
                "file": "README.md/AGENTS.md/CLAUDE.md",
                "line": 0,
                "detail": f"版本号声明不一致: {sorted(unique_versions)}（pyproject.toml 声明: {sorted(declared_versions) or '未知'}）",
            }
        )
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


def list_changed_md_files(base: str, head: str = "HEAD", cwd: Path | None = None) -> list[str]:
    """列出 base..head 之间「仍然存在」的改动 Markdown 文件（仓库相对路径）。

    两个关键参数，缺一不可：

    1. `-z`（NUL 分隔）：不带 `-z` 时，只要 core.quotepath 为 true（git 缺省
       即是），非 ASCII 路径就会被输出成带引号的八进制转义
       （如 "docs/solution/\\346\\236\\266..."）。这种串一旦拼成 Path 就指向
       不存在的文件，调用方会把「改动文档」过滤成空集、静默降级为
       「检查 0 个文档 = 通过」。本仓库文档基本全是中文路径，命中率接近
       100%，该缺陷因此长期掩盖。
    2. `--diff-filter=d`（排除删除）：被删除的文档不可能在工作区遍历结果里
       出现，纳入统计会让调用方的「数量相等」断言误报。小写 d 表示排除
       Deleted，与 `check_docs_naming.py` 的口径一致。

    `-z` 模式下 git 不做任何 quoting，也不依赖用户/CI 的 core.quotepath 配置。

    历史：`docs/verify/pitfalls/python.md` 的 V-034 早已记录此缺陷与修复口径，
    但长期只停留在文档、未落实到代码（当时实测 31 个改动文件只命中 5 个）。
    """
    result = subprocess.run(
        [
            "git",
            "diff",
            "--name-only",
            "-z",
            "--diff-filter=d",
            base,
            head,
            "--",
            "*.md",
        ],
        cwd=cwd or ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return [p for p in result.stdout.split("\0") if p]


def list_content_changed_md_files(base: str, head: str = "HEAD", cwd: Path | None = None) -> set[str]:
    """返回 base..head 之间「确实有行内容变化」的 Markdown 文件集（仓库相对路径）。

    存在的理由：相似度 100% 的 rename 在 `git diff` 里只输出 `rename from/to`，
    **不产生 hunk 头**（没有 `+++ b/` 行），于是行号解析根本看不到它。
    实测一次「18 个纯改名 + 9 个内容改动」的文档 PR：改动文件 26 个，
    hunk 头只有 9 个——若直接拿改动文件全集去和 hunk 头做相等断言，
    正常的纯改名会被误判成「路径 quoting 回归」而 fail-closed。

    `--numstat -z` 一次给出三样东西，正好够用：

    1. 每个文件的新增/删除行数 → 0/0 即纯改名，天然排除；
    2. `-z` 不做任何 quoting，中文路径原样输出（同 `list_changed_md_files`）；
    3. rename 项的路径字段为空，其后紧跟「旧路径」与「新路径」两个 NUL 段，
       取新路径段才是改名后的位置。
    """
    result = subprocess.run(
        [
            "git",
            "diff",
            "--numstat",
            "-z",
            "--diff-filter=d",
            base,
            head,
            "--",
            "*.md",
        ],
        cwd=cwd or ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    parts = result.stdout.split("\0")
    changed: set[str] = set()
    i = 0
    while i < len(parts):
        seg = parts[i]
        if not seg:
            i += 1
            continue
        fields = seg.split("\t")
        if len(fields) < 3:
            i += 1
            continue
        added, deleted, path = fields[0], fields[1], fields[2]
        if path:
            if added != "0" or deleted != "0":
                changed.add(path)
            i += 1
        else:
            # rename/copy：路径被拆到后续两个 NUL 段，new 段才是改名后的路径
            if i + 2 < len(parts):
                new_path = parts[i + 2]
                if new_path and (added != "0" or deleted != "0"):
                    changed.add(new_path)
            i += 3
    return changed


def list_changed_md_lines(base: str, head: str = "HEAD", cwd: Path | None = None) -> dict[str, set[int]]:
    """返回 {仓库相对路径: 本次新增/修改后的行号集合}。

    用途：区分「本次改动引入的腐化」与「文档里的存量腐化」。只对前者阻断，
    否则改一个错别字就要被迫修复整篇文档的历史死链——几千行的设计文档会
    因此没人敢动，反而加速腐化。

    `--unified=0` 让每个 hunk 只覆盖真正变化的行，配合 `@@ -a,b +c,d @@`
    中的 `+c,d` 即可算出新增行区间；纯删除（d=0）不产生区间，因此删除
    操作本身永远不会被误判为「引入问题」。

    路径 quoting：这里必须读取文本 hunk 头，无法用 `-z`，所以显式传
    `-c core.quotepath=false`。但仅靠配置不够可靠——调用方必须用
    `list_changed_md_files`（基于 `-z`，不受配置影响）的结果做「两个解析器
    看到的文件集必须一致」校验，不一致即 fail-closed。
    """
    result = subprocess.run(
        [
            "git",
            "-c",
            "core.quotepath=false",
            "diff",
            "--unified=0",
            "--no-color",
            "--diff-filter=d",
            base,
            head,
            "--",
            "*.md",
        ],
        cwd=cwd or ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    changed: dict[str, set[int]] = {}
    current: str | None = None
    file_re = re.compile(r"^\+\+\+ b/(?P<path>.+)$")
    hunk_re = re.compile(r"^@@ -\d+(?:,\d+)? \+(?P<start>\d+)(?:,(?P<count>\d+))? @@")
    for line in result.stdout.splitlines():
        file_match = file_re.match(line)
        if file_match:
            current = file_match.group("path")
            changed.setdefault(current, set())
            continue
        hunk_match = hunk_re.match(line)
        if hunk_match and current is not None:
            start = int(hunk_match.group("start"))
            count = int(hunk_match.group("count") or 1)
            if count:
                changed[current].update(range(start, start + count))
    return changed


def main() -> None:
    parser = argparse.ArgumentParser(description="文档腐化扫描器")
    parser.add_argument("--dir", default=".", help="扫描目录（相对仓库根，默认全仓库）")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--only", default="C1,C2,C3,C4,C5,C6,C7", help="逗号分隔的检查类别")
    parser.add_argument("--limit", type=int, default=0, help="每类最多输出条数（0 不限）")
    parser.add_argument(
        "--changed-only",
        action="store_true",
        help="仅检查本次 PR 改动的文件（增量门禁，阻断引入新腐化）",
    )
    parser.add_argument(
        "--fail-on-stock",
        action="store_true",
        help="存量问题也阻断（默认仅增量问题阻断，存量只警告）",
    )
    parser.add_argument(
        "--current-blocking",
        action="store_true",
        help="现行全量文档层（非 docs/archive/ 且不含 events/ 目录段）的失效引用不论增量一律阻断",
    )
    args = parser.parse_args()

    scan_root = ROOT / args.dir
    if not scan_root.is_dir():
        print(f"目录不存在: {scan_root}", file=sys.stderr)
        sys.exit(1)

    files = sorted(
        p
        for p in scan_root.rglob("*.md")
        if ".git" not in p.parts
        and "node_modules" not in p.parts
        and ".venv" not in p.parts
        and "__pycache__" not in p.parts
        and ".pytest_cache" not in p.parts
    )

    # 增量门禁：仅保留本次 PR 改动（merge-base..HEAD）的 md 文件
    # 本次新增/修改的行号（按仓库相对路径），用于区分「本次引入」与「存量」
    changed_lines: dict[str, set[int]] | None = None
    if args.changed_only:
        try:
            base = subprocess.run(
                ["git", "merge-base", "HEAD", "origin/main"],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            # 路径解析统一走 list_changed_md_files（内部用 `-z`，详见其 docstring）。
            changed_rel = list_changed_md_files(base)
            changed_files = {str(ROOT / f) for f in changed_rel}
            matched = [f for f in files if str(f) in changed_files]
            # fail-closed：git 报告的改动文档数必须与增量扫描命中数一致。
            # 这是 V-034 明确要求的断言——不对等就意味着有文档被路径解析吞掉，
            # 而「漏检一部分」比「完全不检」更危险：它会输出「✅ 通过」而实际
            # 只检查了一小部分（历史上实测 31 个改动文件只命中 5 个）。
            # 注意 deletions 已由 --diff-filter=d 排除，故此处不存在「删除导致误报」。
            if len(matched) != len(changed_files):
                raise RuntimeError(
                    f"增量扫描命中数 {len(matched)} 与 git 报告的改动文档数 "
                    f"{len(changed_files)} 不一致（疑似路径 quoting 回归）"
                )
            lines_map = list_changed_md_lines(base)
            content_changed = list_content_changed_md_files(base)
            # 两个解析器必须覆盖同一批「有内容变化」的文件。
            # 断言对象是 numstat 的「真有几行变化」集合而非改动文件全集：
            # 纯 rename 不产生 hunk 头，天然没有行号可归因，不应被视为丢文件。
            # 若 hunk 头路径被 quoting 污染，它会变成一个不在 changed_rel 里的
            # 野指针，下面的相等断言依旧会 fail-closed。
            if set(lines_map) != content_changed:
                raise RuntimeError(
                    f"行号解析看到的文件集（{len(lines_map)}）与 numstat 报告的"
                    f"「有内容变化」文件集（{len(content_changed)}）"
                    f"不一致（疑似 hunk 头路径 quoting 回归）"
                )
            files = matched
            changed_lines = lines_map
        except Exception as e:  # noqa: BLE001
            # 失败关闭（fail-closed）：无法确定「本次改动了什么」时，既不能
            # 静默降级为全量扫描，也不能当作通过。
            # 降级为全量是陷阱：全量模式下 changed_lines 为 None，所有存量
            # 腐化都会落到「仅警告」分支，最终依旧输出「✅ 门禁通过」——
            # 这与本脚本要修的空转是同一种失效，只是换了个入口。
            print(f"❌ 无法确定本次改动的文档，增量门禁失败关闭: {e}", file=sys.stderr)
            print(
                "   常见原因：本地缺少 origin/main（请先 git fetch），或 git 路径解析回归。确认无改动可改用全量扫描。",
                file=sys.stderr,
            )
            sys.exit(1)
        print(f"增量模式：仅检查 {len(files)} 个改动文档")

    enabled = [c.strip() for c in args.only.split(",") if c.strip()]
    all_issues: list[dict] = []
    for cat in enabled:
        if cat not in CHECKERS:
            continue
        # C7 版本一致性检查不依赖 files 参数
        issues = CHECKERS[cat]() if cat == "C7" else CHECKERS[cat](files)
        waived = [it for it in issues if it.get("waived")]
        blocking = [it for it in issues if not it.get("waived")]
        print(f"\n[{cat}] 发现 {len(blocking)} 个问题")
        shown = blocking[: args.limit] if args.limit else blocking
        for it in shown:
            print(f"  {it['file']}:{it['line']}  {it['detail']}")
        if waived:
            # 豁免必须显式列出来：隐式放宽等于把门禁降级为宽松模式，
            # 修正会因此退化成空转（同一失效在 V-035 里已经记过一笔）。
            print(f"[{cat}] 另有 {len(waived)} 处经语境/状态判定为豁免，不阻断（逐条列出供人工复核）：")
            for it in waived:
                print(f"  ~ {it['file']}:{it['line']}  {it['detail']}  ← {it['waive_reason']}")
        all_issues.extend(issues)

    print("\n" + "=" * 60)
    print("汇总：")
    hard_issues = [i for i in all_issues if not i.get("waived")]
    n_waived = len(all_issues) - len(hard_issues)
    for cat in enabled:
        n = len([i for i in hard_issues if i["type"] == cat])
        print(f"  {cat}: {n}")
    print(f"  总计: {len(hard_issues)}")
    if n_waived:
        print(f"  语境豁免（已显式列出，不计入总计）: {n_waived}")

    if args.json:
        print("\nJSON:")
        print(json.dumps(all_issues, ensure_ascii=False, indent=1))

    # 分级门禁：
    #  - 密钥泄露（C6）永远阻断（无论增量/存量），安全红线不可妥协
    #  - 失效引用（C1/C2/C3）：增量模式按「行」判断，只阻断落在本次新增/
    #    修改行上的新问题；改动文档其他位置的历史腐化属存量，仅警告
    #  - 其余类别（C4/C5/C7）只报告不阻断
    secret_block = [i for i in all_issues if i["type"] == "C6"]
    rot_block = [i for i in all_issues if i["type"] in ("C1", "C2", "C3") and not i.get("waived")]

    if secret_block:
        print(f"\n❌ 发现 {len(secret_block)} 个密钥泄露（C6），安全红线，PR 禁止合并")
        sys.exit(1)

    if rot_block:
        # 现行层升为阻断级：该层已归零，任何一条都是新引入的故障。
        # 事件层/归档层继续走增量判定——它们是史料，不得为了变绿而改写。
        blocking, extra, stock = select_blocking(
            rot_block,
            changed_lines,
            changed_only=args.changed_only,
            fail_on_stock=args.fail_on_stock,
            current_blocking=args.current_blocking,
        )
        if blocking and not (args.changed_only and not args.fail_on_stock):
            print(f"\n❌ 发现 {len(blocking)} 个阻断级失效引用（C1/C2/C3），PR 禁止合并")
            sys.exit(1)
        if blocking:
            # 归因必须分开写：全部条目都是现行层存量时却报「本次改动行引入」，
            # 会把一个与 PR 无关的历史缺陷算到作者头上，误导修复方向。
            from_lines = len(blocking) - len(extra)
            print(
                f"\n❌ 发现 {len(blocking)} 个阻断级失效引用（C1/C2/C3）："
                f"本次改动行 {from_lines} 个、现行层存量 {len(extra)} 个，PR 禁止合并"
            )
            for it in blocking:
                print(f"   {it['file']}:{it['line']}  {it['detail']}")
            if extra:
                print("   其中现行全量文档层的条目不论是否本次改动均阻断（该层已归零）")
            if stock:
                print(f"   另有 {stock} 个位于事件/归档层的其他行，属史料存量，本步不阻断")
            sys.exit(1)
        print(f"\n⚠️ 发现 {len(rot_block)} 个失效引用（C1/C2/C3），均不在本次改动行且不属于现行层，仅警告不阻断")
        print("   治理口径见 docs/solution/events/2026-10-26-文档瘦身Wave3现行文档准确性治理.md")

    print("\n✅ 门禁通过（无新增密钥泄露与失效引用）")
    sys.exit(0)


if __name__ == "__main__":
    main()
