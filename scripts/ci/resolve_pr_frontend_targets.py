#!/usr/bin/env python3
"""根据 PR 变更生成保守的前端测试与构建范围。"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

FRONTEND_PACKAGES = ("admin", "customer")
FULL_FRONTEND_FILES = {
    "frontend/package.json",
    "frontend/pnpm-lock.yaml",
    "frontend/pnpm-workspace.yaml",
    "frontend/.npmrc",
}


@dataclass(frozen=True)
class FrontendPlan:
    """前端门禁范围及其决策原因。"""

    scope: str
    reason: str


def resolve_frontend_plan(changed_files: list[str]) -> FrontendPlan:
    """从变更路径推导最小安全范围；无法确定时回退完整构建。"""
    paths = sorted({path.strip() for path in changed_files if path.strip()})
    if not paths:
        return FrontendPlan("full", "无法获得 PR 变更清单，按失败关闭策略运行完整前端回归")

    packages: set[str] = set()
    for path in paths:
        if path in FULL_FRONTEND_FILES or path.startswith("frontend/shared/"):
            return FrontendPlan("full", "workspace、锁文件或共享包变更需要完整前端回归")
        for package in FRONTEND_PACKAGES:
            prefix = f"frontend/{package}/"
            if path.startswith(prefix):
                packages.add(package)
                break
        else:
            if path.startswith("frontend/"):
                return FrontendPlan("full", "无法映射前端变更到单一应用，运行完整前端回归")

    if not packages:
        return FrontendPlan("none", "本次 PR 未修改前端应用")
    if len(packages) == len(FRONTEND_PACKAGES):
        return FrontendPlan("full", "customer 与 admin 同时变更，运行完整前端回归")
    package = next(iter(packages))
    return FrontendPlan(package, f"仅运行变更直接覆盖的 {package} 前端应用")


def _write_github_output(plan: FrontendPlan, output_path: Path) -> None:
    """写入 GitHub Actions 输出。"""
    with output_path.open("a", encoding="utf-8") as output:
        output.write(f"scope={plan.scope}\n")
        output.write(f"reason={plan.reason}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="解析 PR 前端测试与构建范围")
    parser.add_argument("--changed-files", type=Path, required=True, help="每行一个变更文件")
    parser.add_argument("--github-output", type=Path, help="GitHub Actions 输出文件")
    args = parser.parse_args()

    plan = resolve_frontend_plan(args.changed_files.read_text(encoding="utf-8").splitlines())
    if args.github_output:
        _write_github_output(plan, args.github_output)
        return
    print(plan.scope)
    print(plan.reason)


if __name__ == "__main__":
    main()
