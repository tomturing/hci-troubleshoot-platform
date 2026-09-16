#!/usr/bin/env python3
"""判定 main push 是否需要运行后端质量门禁层。

背景：镜像构建已按 Dockerfile 实际输入收敛（resolve_image_build_plan.py），
但 Lint / unit-tests / integration-tests / security-tests 对每次 main push
仍然无条件全量执行，且发布链（prepare → 构建并推送镜像 → 晋级非生产环境）
把这三个测试 job 的 success 作为前置条件。结果是只改 admin UI 的 push 也要为
8 个后端服务的完整回归多等约 4 分钟，而这批回归的输入完全没有变化。

判定采用「白名单 + 失败关闭」：
  - 只有当本次 push 的全部变更都落在"不可能改变后端门禁输入"的白名单内，
    才允许跳过后端门禁层；
  - 变更清单为空、出现任何白名单之外的路径，一律要求完整后端回归。

白名单只收 frontend/ 与 docs/ 以及根级说明文档：这些路径不被 Lint、unit-tests
（含 make config-contract-check、hci_sim 与 offline-collector 的 Go 运行时测试）、
integration-tests、security-tests 的任何步骤读取。deploy/ 会被 unit-tests 中的
REQUIRE_HELM 配置契约校验读取，因此不在白名单内。
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

# 变更全部落在这里面才允许跳过后端门禁层；扩展白名单必须同时确认对应 job
# 的每一步都不读取该路径。
NO_BACKEND_GATE_PREFIXES = ("frontend/", "docs/")
NO_BACKEND_GATE_FILES = frozenset({"README.md", "AGENTS.md", "CLAUDE.md"})


@dataclass(frozen=True)
class GateScope:
    """后端门禁层的必要性及其决策原因。"""

    required: bool
    reason: str


def _is_no_backend_gate_impact(path: str) -> bool:
    """判断单条变更路径是否不可能改变后端门禁的输入。"""
    normalized = path.lstrip("./")
    if normalized in NO_BACKEND_GATE_FILES:
        return True
    return normalized.startswith(NO_BACKEND_GATE_PREFIXES)


def resolve_gate_scope(changed_files: list[str]) -> GateScope:
    """从主干变更清单推导后端门禁范围；不确定时绝不跳过。"""
    paths = sorted({path.strip() for path in changed_files if path.strip()})
    if not paths:
        return GateScope(True, "无法获得主干变更清单，按失败关闭策略运行完整后端回归")

    outside = [path for path in paths if not _is_no_backend_gate_impact(path)]
    if outside:
        return GateScope(True, f"变更包含后端门禁输入路径 {outside[0]}，运行完整后端回归")

    return GateScope(False, f"变更 {len(paths)} 个文件全部落在 frontend/docs 白名单内，跳过后端质量门禁层")


def _write_github_output(scope: GateScope, output_path: Path) -> None:
    """写入 GitHub Actions 输出。"""
    with output_path.open("a", encoding="utf-8") as output:
        output.write(f"required={'true' if scope.required else 'false'}\n")
        output.write(f"reason={scope.reason}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="解析主干后端门禁影响范围")
    parser.add_argument("--changed-files", type=Path, required=True, help="每行一个相对仓库根目录的变更文件")
    parser.add_argument("--github-output", type=Path, help="GitHub Actions 输出文件")
    args = parser.parse_args()

    changed_files = args.changed_files.read_text(encoding="utf-8").splitlines()
    scope = resolve_gate_scope(changed_files)
    print(f"主干后端门禁范围：{'required' if scope.required else 'not-required'}")
    print(f"原因：{scope.reason}")
    if args.github_output:
        _write_github_output(scope, args.github_output)


if __name__ == "__main__":
    main()
