#!/usr/bin/env python3
"""
docs/ 目录命名规范检查脚本

命名规则（来源：docs/文档管理规范.md 第十一节）：
  - solution/ 主干：{中文}设计.md（无版本号、无点分隔符）
  - task/ 主干：{中文}任务.md
  - solution/ 分支：{目录名}设计.md（文件名直接使用模块目录名）
  - task/ 分支：{目录名}任务.md
  - 分支目录名：全小写英文（a-z、0-9、连字符）
  - 禁止：泛化名称 设计.md/任务.md、带点分隔符（如 工单.设计.md）、带版本号

用法：
  python scripts/check_docs_naming.py             # PR 模式：仅检查变更文件
  python scripts/check_docs_naming.py --full      # 全量检查 docs/

环境变量：
  BASE_SHA / HEAD_SHA   PR 模式下 git diff 范围（CI 注入）
  NAMING_BYPASS=1       PR 含 naming-approved 标签时跳过检查（CI 注入）
"""

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

# ─────────────────────────── 常量定义 ───────────────────────────

DOCS_BASE = Path("docs")

# 不触发检查的特殊目录
SKIP_DIRS = {"events", "pitfalls", "archive", ".git"}

# 分支目录名规范：全小写英文字母、数字、连字符
VALID_DIR_RE = re.compile(r'^[a-z][a-z0-9-]*$')

# 泛化名称（分支目录下禁止使用）
FORBIDDEN_GENERIC = {"设计.md", "任务.md"}

# 禁止的文件名模式（正则 + 说明）
FORBIDDEN_PATTERNS = [
    # 带点分隔符：工单.设计.md、架构.任务.md
    (re.compile(r'[\u4e00-\u9fff\w]+\.[设任]\S+\.md$'), "带点分隔符（如 工单.设计.md）"),
    # 带版本号：架构设计v2.md、接口设计_v3.md
    (re.compile(r'v\d+(?:[._]\d+)*\.md$', re.IGNORECASE), "带版本号（如 架构设计v2.md）"),
    (re.compile(r'_v\d+\.md$', re.IGNORECASE), "带版本号后缀（如 接口设计_v3.md）"),
]

# 分支目录内允许存在的"例外辅助文档"（不受主设计/任务命名约束）
ALLOWED_EXTRAS = {
    "评分机制.md",
    "SSH终端交互.md",
    "SOP多叉决策树设计.md",
    "图片识别.md",
}

# ── 核心命名白名单 ────────────────────────────────────────────────
# 分支目录 → 允许的主文档文件名干（不含 设计.md / 任务.md 后缀）
# 规则：统一使用中文模块名，目录名（英文）与文件名（中文）解耦
# 此白名单是命名规范的机器可读形式，新增分支时同步更新此处
BRANCH_APPROVED_STEMS: dict[str, list[str]] = {
    "case":           ["工单"],
    "conversation":   ["对话"],
    "agent":          ["AI助手", "agent"],  # 2026-05-20 由 ai-assistant 重命名
    "knowledge-base": ["知识库", "SOP树"],
    "custom-ui":      ["客户端"],
    "admin-ui":       ["管理台"],
}


# ─────────────────────────── 工具函数 ───────────────────────────

def get_pr_changed_docs() -> list[Path]:
    """获取 PR 中变更的 docs/*.md 文件列表"""
    base = os.environ.get("BASE_SHA", "")
    head = os.environ.get("HEAD_SHA", "")
    cmd = ["git", "-c", "core.quotepath=false", "diff", "--name-only"]
    if base and head:
        cmd += [base, head]
    else:
        cmd += ["HEAD"]
    result = subprocess.run(cmd, capture_output=True, text=True)
    paths = []
    for f in result.stdout.splitlines():
        f = f.strip()
        if f.startswith("docs/") and f.endswith(".md"):
            paths.append(Path(f))
    return paths


def should_skip(path: Path) -> bool:
    """该路径是否应跳过检查（events/pitfalls/archive 等不受命名规范约束）"""
    return any(part in SKIP_DIRS for part in path.parts)


# ─────────────────────────── 单文件检查 ───────────────────────────

def check_file(path: Path) -> list[str]:
    """
    检查单个 .md 文件是否符合命名规范。
    返回错误信息列表（空表示通过）。
    """
    errors: list[str] = []
    if should_skip(path):
        return errors

    parts = path.parts
    # 需要至少 docs/{stage}/{file} 三层
    if len(parts) < 3:
        return errors

    stage = parts[1]  # solution / task
    if stage not in ("solution", "task"):
        return errors

    fname = path.name
    suffix = "设计" if stage == "solution" else "任务"

    # ① 泛化文件名检查（分支目录内不能叫 设计.md 或 任务.md）
    if fname in FORBIDDEN_GENERIC and len(parts) >= 4:
        dir_name = parts[2]
        approved = BRANCH_APPROVED_STEMS.get(dir_name, [dir_name])
        example = f"{approved[0]}{suffix}.md"
        errors.append(
            f"❌ {path}\n"
            f"   泛化文件名 '{fname}'，应改为 '{example}'"
        )
        return errors  # 已泛化，不叠加其他错误

    # ② 禁止模式检查（带点分隔符、带版本号）
    for pattern, desc in FORBIDDEN_PATTERNS:
        if pattern.search(fname):
            errors.append(f"❌ {path}\n   {desc}")

    # ③ 分支目录内主文档：文件名干必须在白名单内
    #    目标：docs/{stage}/{branch}/{file}.md（刚好 4 层）
    if len(parts) == 4:
        branch_dir = parts[2]
        approved_stems = BRANCH_APPROVED_STEMS.get(branch_dir)
        if approved_stems is not None and fname not in ALLOWED_EXTRAS:
            # 只检查以正确后缀结尾的文件（如 *设计.md）
            if fname.endswith(f"{suffix}.md"):
                stem = fname[: -len(f"{suffix}.md")]
                if stem not in approved_stems:
                    examples = "、".join(f"'{s}{suffix}.md'" for s in approved_stems)
                    errors.append(
                        f"⚠️  {path}\n"
                        f"   文件名干 '{stem}' 不在白名单内，\n"
                        f"   '{branch_dir}/' 目录允许的主文档名：{examples}"
                    )

    return errors


# ─────────────────────────── 目录名检查 ───────────────────────────

def check_dir_names() -> list[str]:
    """检查 solution/ 和 task/ 一级子目录名是否符合规范（全小写英文）"""
    errors: list[str] = []
    for stage in ("solution", "task"):
        stage_path = DOCS_BASE / stage
        if not stage_path.exists():
            continue
        for d in sorted(stage_path.iterdir()):
            if not d.is_dir() or d.name in SKIP_DIRS:
                continue
            if not VALID_DIR_RE.match(d.name):
                errors.append(
                    f"❌ 目录 {d}\n"
                    f"   目录名 '{d.name}' 不符合规范，应为全小写英文（字母/数字/连字符），\n"
                    f"   如：case、agent、custom-ui"
                )
    return errors


# ─────────────────────────── 主检查模式 ───────────────────────────

def run_pr_check() -> list[str]:
    """PR 模式：仅检查 PR 变更的 docs/*.md 文件 + 全量目录名检查"""
    changed = get_pr_changed_docs()
    errors: list[str] = []
    for path in changed:
        errors.extend(check_file(path))
    errors.extend(check_dir_names())
    return errors


def run_full_check() -> list[str]:
    """全量模式：检查 docs/ 下所有 .md 文件"""
    errors: list[str] = []
    for path in sorted(DOCS_BASE.rglob("*.md")):
        errors.extend(check_file(path))
    errors.extend(check_dir_names())
    return errors


# ─────────────────────────── 入口 ───────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="docs/ 命名规范检查")
    parser.add_argument(
        "--full", action="store_true",
        help="全量检查 docs/（默认仅检查 PR 变更文件）"
    )
    args = parser.parse_args()

    # 旁路：PR 含 naming-approved 标签时跳过
    if os.environ.get("NAMING_BYPASS") == "1":
        print("ℹ️  检测到 naming-approved 标签，跳过命名规范检查")
        sys.exit(0)

    errors = run_full_check() if args.full else run_pr_check()

    if not errors:
        print("✅ 文档命名规范检查通过")
        sys.exit(0)

    print("=" * 60)
    print("📋 文档命名规范检查 — 发现以下问题：")
    print("=" * 60)
    for e in errors:
        print(e)
    print()
    print("─" * 60)
    print("修复方式（二选一）：")
    print("  1. 按上方提示重命名文件后重新推送")
    print("  2. 特殊情况：在 PR 添加 'naming-approved' 标签并在描述中说明原因")
    print()
    print("命名规范速查：")
    print("  solution 分支：docs/solution/{英文目录}/{中文模块名}设计.md")
    print("  task    分支：docs/task/{英文目录}/{中文模块名}任务.md")
    print("  示例：case/工单设计.md、custom-ui/客户端设计.md、admin-ui/管理台设计.md")
    print("─" * 60)
    sys.exit(1)


if __name__ == "__main__":
    main()
