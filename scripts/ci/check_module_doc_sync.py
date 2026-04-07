#!/usr/bin/env python3
"""
模块代码 ↔ 文档同步检查脚本

规则：当 PR 修改了某个服务/模块的代码文件时，对应的设计文档或任务文档也应更新。
若代码有实质性变更但无对应文档更新，则输出提示并以 exit(1) 阻断合并。

代码模块 → 对应文档 映射：
  backend/case-service/         → solution/case/工单设计.md        + task/case/工单任务.md
  backend/conversation-service/ → solution/conversation/对话设计.md + task/conversation/对话任务.md
  backend/kb-service/           → solution/knowledge-base/知识库设计.md + task/knowledge-base/知识库任务.md
  backend/scheduler-service/    → solution/ai-assistant/AI助手设计.md  + task/ai-assistant/AI助手任务.md
  backend/api-gateway/          → solution/架构设计.md              + task/架构任务.md
  backend/shared/               → solution/架构设计.md              （共享库影响全局架构）
  frontend/customer/            → solution/custom-ui/custom-ui设计.md + task/custom-ui/custom-ui任务.md
  frontend/admin/               → solution/admin-ui/admin-ui设计.md   + task/admin-ui/admin-ui任务.md
  database/                     → solution/数据库设计.md            + task/数据库任务.md
  deploy/helm/                  → deploy/部署设计.md

旁路：
  - PR 描述包含 [skip doc-sync]
  - PR 含 doc-update-exempt 标签（CI 设置 DOC_SYNC_BYPASS=1）

用法：
  BASE_SHA=xxx HEAD_SHA=yyy python scripts/check_module_doc_sync.py
"""

import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path


# ─────────────────────────── 映射定义 ───────────────────────────

@dataclass
class ModuleMapping:
    """代码模块 → 对应文档 的映射关系"""
    # 代码路径前缀（用于 startswith 匹配）
    code_prefix: str
    # 模块友好名称（用于错误提示）
    display_name: str
    # 对应的 solution 设计文档（相对于项目根）
    solution_doc: str | None
    # 对应的 task 任务文档
    task_doc: str | None
    # 允许不触发检查的路径片段（测试目录等）
    exempt_paths: tuple[str, ...] = field(default_factory=lambda: (
        "/tests/", "/test/", "/__pycache__/",
    ))
    # 允许不触发检查的文件名（纯依赖/配置）
    exempt_filenames: frozenset[str] = field(default_factory=lambda: frozenset({
        "pyproject.toml", "requirements.txt", "package.json",
        "pnpm-lock.yaml", "uv.lock", ".env.example", "alembic.ini",
        "conftest.py", "Makefile",
    }))
    # 允许不触发检查的文件后缀
    exempt_suffixes: tuple[str, ...] = field(default_factory=lambda: (
        ".test.ts", ".spec.ts", ".test.py", "_test.py",
        ".lock", ".json",   # 依赖锁和配置 JSON
    ))


# 映射表（顺序影响匹配优先级，更具体的放前面）
MODULE_MAPPINGS: list[ModuleMapping] = [
    ModuleMapping(
        code_prefix="backend/case-service/",
        display_name="工单服务（case-service）",
        solution_doc="docs/solution/case/工单设计.md",
        task_doc="docs/task/case/工单任务.md",
    ),
    ModuleMapping(
        code_prefix="backend/conversation-service/",
        display_name="对话服务（conversation-service）",
        solution_doc="docs/solution/conversation/对话设计.md",
        task_doc="docs/task/conversation/对话任务.md",
    ),
    ModuleMapping(
        code_prefix="backend/kb-service/",
        display_name="知识库服务（kb-service）",
        solution_doc="docs/solution/knowledge-base/知识库设计.md",
        task_doc="docs/task/knowledge-base/知识库任务.md",
    ),
    ModuleMapping(
        code_prefix="backend/scheduler-service/",
        display_name="AI 调度服务（scheduler-service）",
        solution_doc="docs/solution/ai-assistant/AI助手设计.md",
        task_doc="docs/task/ai-assistant/AI助手任务.md",
    ),
    ModuleMapping(
        code_prefix="backend/api-gateway/",
        display_name="API 网关（api-gateway）",
        solution_doc="docs/solution/架构设计.md",
        task_doc="docs/task/架构任务.md",
    ),
    ModuleMapping(
        code_prefix="backend/shared/",
        display_name="共享库（backend/shared）",
        solution_doc="docs/solution/架构设计.md",
        task_doc=None,
    ),
    ModuleMapping(
        code_prefix="frontend/customer/",
        display_name="客户端 UI（frontend/customer）",
        solution_doc="docs/solution/custom-ui/客户端设计.md",
        task_doc="docs/task/custom-ui/客户端任务.md",
    ),
    ModuleMapping(
        code_prefix="frontend/admin/",
        display_name="管理台 UI（frontend/admin）",
        solution_doc="docs/solution/admin-ui/管理台设计.md",
        task_doc="docs/task/admin-ui/管理台任务.md",
    ),
    ModuleMapping(
        code_prefix="database/",
        display_name="数据库（database）",
        solution_doc="docs/solution/数据库设计.md",
        task_doc="docs/task/数据库任务.md",
    ),
    ModuleMapping(
        code_prefix="deploy/helm/",
        display_name="Helm 部署配置（deploy/helm）",
        solution_doc="docs/deploy/部署设计.md",
        task_doc=None,
    ),
]


# ─────────────────────────── 工具函数 ───────────────────────────

def get_changed_files() -> list[str]:
    """获取 PR 与 base 之间变更的文件列表"""
    base = os.environ.get("BASE_SHA", "")
    head = os.environ.get("HEAD_SHA", "")
    cmd = ["git", "-c", "core.quotepath=false", "diff", "--name-only"]
    if base and head:
        cmd += [base, head]
    else:
        cmd += ["HEAD"]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return [f.strip() for f in result.stdout.splitlines() if f.strip()]


def is_meaningful_change(fpath: str, mapping: ModuleMapping) -> bool:
    """
    判断文件变更是否"有意义"（需要文档更新）。
    测试文件、配置文件、依赖锁文件等不需要触发文档检查。
    """
    for exempt_path in mapping.exempt_paths:
        if exempt_path in fpath:
            return False

    fname = Path(fpath).name
    if fname in mapping.exempt_filenames:
        return False

    for suffix in mapping.exempt_suffixes:
        if fpath.endswith(suffix):
            return False

    return True


def check_bypass() -> bool:
    """检查是否命中任一旁路条件"""
    # 标签旁路（CI 注入 DOC_SYNC_BYPASS=1）
    if os.environ.get("DOC_SYNC_BYPASS") == "1":
        print("ℹ️  检测到 doc-update-exempt 标签，跳过模块文档同步检查")
        return True
    # PR body 内联旁路
    pr_body = os.environ.get("PR_BODY", "")
    if "[skip doc-sync]" in pr_body:
        print("ℹ️  PR 描述包含 [skip doc-sync]，跳过模块文档同步检查")
        return True
    return False


# ─────────────────────────── 主逻辑 ───────────────────────────

def main() -> None:
    if check_bypass():
        sys.exit(0)

    changed_files = get_changed_files()
    if not changed_files:
        print("无变更文件，跳过模块文档同步检查")
        sys.exit(0)

    print(f"本次 PR 变更文件（共 {len(changed_files)} 个）")

    # 收集各模块的有意义变更
    # code_prefix → (mapping, [有意义变更的代码文件])
    touched: dict[str, tuple[ModuleMapping, list[str]]] = {}
    for fpath in changed_files:
        for mapping in MODULE_MAPPINGS:
            if fpath.startswith(mapping.code_prefix) and is_meaningful_change(fpath, mapping):
                if mapping.code_prefix not in touched:
                    touched[mapping.code_prefix] = (mapping, [])
                touched[mapping.code_prefix][1].append(fpath)
                break

    if not touched:
        print("✅ 未检测到需要文档同步的模块改动（仅测试/配置变更）")
        sys.exit(0)

    # 检查各模块对应的文档是否也有更新（solution 或 task doc 更新其一即通过）
    failures: list[tuple[ModuleMapping, list[str], list[str]]] = []
    for code_prefix, (mapping, code_files) in touched.items():
        expected_docs = [d for d in [mapping.solution_doc, mapping.task_doc] if d]
        doc_updated = any(doc in changed_files for doc in expected_docs)
        if not doc_updated:
            failures.append((mapping, code_files, expected_docs))

    if not failures:
        print("✅ 所有模块的对应文档均已更新")
        sys.exit(0)

    # 输出失败报告
    print()
    print("=" * 65)
    print("📋 模块文档同步检查 — 以下模块代码有变更但对应文档未更新：")
    print("=" * 65)

    for mapping, code_files, expected_docs in failures:
        print(f"\n🔸 模块：{mapping.display_name}")
        print("   修改的代码文件：")
        for f in code_files[:6]:
            print(f"     - {f}")
        if len(code_files) > 6:
            print(f"     ... 还有 {len(code_files) - 6} 个文件")
        print("   需要更新的文档（更新其中至少一项）：")
        for doc in expected_docs:
            exists_marker = "✓ 存在" if Path(doc).exists() else "✗ 不存在"
            print(f"     - {doc}  [{exists_marker}]")

    print()
    print("─" * 65)
    print("修复方式：")
    print("  1. 更新上方列出的对应文档后重新推送")
    print("  2. 若改动仅为 typo/格式/测试调整，无需文档更新时：")
    print("     在 PR 描述中添加  [skip doc-sync]")
    print("     或在 PR 添加 'doc-update-exempt' 标签")
    print("─" * 65)
    sys.exit(1)


if __name__ == "__main__":
    main()
