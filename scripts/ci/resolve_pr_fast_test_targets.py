#!/usr/bin/env python3
"""根据 PR 变更生成保守的 Python 快速测试计划。"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

MAX_FAST_TARGETS = 2
GLOBAL_DEPENDENCY_PREFIXES = (
    "backend/shared/",
    "backend/conftest.py",
    "tests/conftest.py",
)
GLOBAL_DEPENDENCY_FILES = {
    "Makefile",
    "conftest.py",
    "pyproject.toml",
    "uv.lock",
    "scripts/ci/resolve_pr_fast_test_targets.py",
}


@dataclass(frozen=True)
class TestPlan:
    """快速门禁的测试范围及其决策原因。"""

    mode: str
    targets: tuple[str, ...]
    reason: str


def _is_unit_test(path: str) -> bool:
    """仅选择能由 unit-tests 负责的测试文件。"""
    return (
        (
            path.startswith("tests/unit/")
            or "/tests/unit/" in path
            or (path.startswith("backend/") and "/tests/" in path and "/tests/integration/" not in path)
        )
        and Path(path).name.startswith("test_")
        and path.endswith(".py")
    )


def _service_test_candidates(repo_root: Path, changed_path: str) -> list[str]:
    """为服务源码寻找可证明对应的测试；找不到时由调用方升级为完整回归。"""
    parts = Path(changed_path).parts
    if len(parts) < 4 or parts[0] != "backend" or parts[2] != "app":
        return []

    service = parts[1]
    stem = Path(changed_path).stem
    tests_root = repo_root / "backend" / service / "tests"
    if not tests_root.is_dir():
        return []

    app_parts = (*parts[3:-1], stem)
    candidate_names = {f"test_{stem}.py"}
    # 测试文件有时保留领域前缀，例如 app/tools/qfk/ai_extractor.py
    # 对应 test_qfk_ai_extractor.py。只接受仓库中实际存在的文件，避免猜测。
    candidate_names.update(f"test_{'_'.join(app_parts[start:])}.py" for start in range(len(app_parts) - 1))
    candidates = {
        path.relative_to(repo_root).as_posix()
        for name in candidate_names
        for path in tests_root.rglob(name)
        if "integration" not in path.parts
    }
    return sorted(candidates)


def resolve_test_plan(changed_files: list[str], repo_root: Path) -> TestPlan:
    """从最小可验证集合推导计划；不确定依赖时绝不猜测。"""
    normalized = sorted({path.strip() for path in changed_files if path.strip()})
    if not normalized:
        return TestPlan("full", (), "无法获得 PR 变更清单，按失败关闭策略运行完整回归")

    if any(
        path in GLOBAL_DEPENDENCY_FILES
        or path.endswith("/pyproject.toml")
        or path.endswith("/uv.lock")
        or path.startswith(GLOBAL_DEPENDENCY_PREFIXES)
        or path.startswith(".github/workflows/")
        for path in normalized
    ):
        return TestPlan("full", (), "共享依赖、测试基础设施或工作流变更需要完整回归")

    targets: set[str] = set()
    for path in normalized:
        if _is_unit_test(path):
            targets.add(path)
            continue

        if path.startswith("backend/") and path.endswith(".py"):
            candidates = _service_test_candidates(repo_root, path)
            if not candidates:
                return TestPlan("full", (), "无法建立后端源码到单元测试的安全映射")
            targets.update(candidates)

    if not targets:
        return TestPlan("none", (), "本次 PR 未修改 Python 单元测试或后端源码")
    if len(targets) > MAX_FAST_TARGETS:
        return TestPlan("full", (), f"受影响测试超过 {MAX_FAST_TARGETS} 个，升级为完整回归")

    return TestPlan("targeted", tuple(sorted(targets)), "仅运行变更直接覆盖的单元测试")


def _write_github_output(plan: TestPlan, output_path: Path) -> None:
    """使用 GitHub Actions 多行输出协议传递测试目标。"""
    delimiter = "HCI_FAST_TEST_TARGETS"
    with output_path.open("a", encoding="utf-8") as output:
        output.write(f"mode={plan.mode}\n")
        output.write(f"reason={plan.reason}\n")
        output.write(f"targets<<{delimiter}\n")
        output.write("\n".join(plan.targets))
        output.write(f"\n{delimiter}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="解析 PR 快速单元测试目标")
    parser.add_argument("--changed-files", type=Path, required=True, help="每行一个相对仓库根目录的变更文件")
    parser.add_argument("--repo-root", type=Path, default=Path.cwd(), help="仓库根目录")
    parser.add_argument("--github-output", type=Path, help="GitHub Actions 输出文件")
    args = parser.parse_args()

    changed_files = args.changed_files.read_text(encoding="utf-8").splitlines()
    plan = resolve_test_plan(changed_files, args.repo_root.resolve())
    if args.github_output:
        _write_github_output(plan, args.github_output)
        return

    print(plan.mode)
    print(plan.reason)
    print("\n".join(plan.targets))


if __name__ == "__main__":
    main()
