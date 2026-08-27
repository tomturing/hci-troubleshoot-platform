#!/usr/bin/env python3
"""根据 PR 变更生成保守的 Python 快速测试计划。"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

MAX_FAST_SERVICES = 1
MAX_FAST_TARGETS = 8
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

# 信号公共层拥有独立、可审计的消费者测试集合。只有 PR 的所有代码变更都
# 落在此白名单内时才启用快速路径；混入任意未知共享代码仍然完整回归。
SIGNAL_FAST_PATH_FILES = {
    "backend/agent-service/app/adapters/agents/htp/kbd_differential.py",
    "backend/agent-service/app/routes/signal_dry_run.py",
    "backend/agent-service/app/tools/qfk/engine.py",
    "backend/agent-service/app/tools/qkv/engine.py",
    "backend/agent-service/tests/conftest.py",
    "backend/agent-service/tests/unit/test_kbd_differential.py",
    "backend/agent-service/tests/unit/test_qfk_ai_extractor.py",
    "backend/agent-service/tests/unit/test_qkv.py",
    "backend/agent-service/tests/unit/test_signal_dry_run.py",
    "backend/shared/tests/test_ai_extractor.py",
    "backend/shared/tests/test_ai_processing.py",
    "backend/shared/tests/test_qkv_output_processing.py",
}
SIGNAL_FAST_PATH_PREFIXES = ("backend/shared/signals/",)
SIGNAL_FAST_TEST_TARGETS = (
    "backend/shared/tests/test_ai_extractor.py",
    "backend/shared/tests/test_ai_processing.py",
    "backend/shared/tests/test_qkv_output_processing.py",
    "backend/agent-service/tests/unit/test_kbd_differential.py",
    "backend/agent-service/tests/unit/test_qfk_ai_extractor.py",
    "backend/agent-service/tests/unit/test_qkv.py",
    "backend/agent-service/tests/unit/test_signal_dry_run.py",
)


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


def _service_test_target(repo_root: Path, changed_path: str) -> str | None:
    """按服务选择单测目录，不依赖源码与测试文件的命名关系。"""
    parts = Path(changed_path).parts
    if len(parts) < 4 or parts[0] != "backend" or parts[2] != "app":
        return None

    service = parts[1]
    tests_root = repo_root / "backend" / service / "tests"
    if not tests_root.is_dir():
        return None

    unit_root = tests_root / "unit"
    if not unit_root.is_dir():
        # 根 tests 目录通常同时包含 integration，无法安全缩小范围。
        return None
    return unit_root.relative_to(repo_root).as_posix()


def _is_signal_fast_path(normalized: list[str]) -> bool:
    """判断变更是否严格属于已审计的信号公共层快速范围。"""
    code_paths = [path for path in normalized if not path.startswith("docs/")]
    if not code_paths or not any(path.startswith(SIGNAL_FAST_PATH_PREFIXES) for path in code_paths):
        return False
    return all(path in SIGNAL_FAST_PATH_FILES or path.startswith(SIGNAL_FAST_PATH_PREFIXES) for path in code_paths)


def resolve_test_plan(changed_files: list[str], repo_root: Path) -> TestPlan:
    """从最小可验证集合推导计划；不确定依赖时绝不猜测。"""
    normalized = sorted({path.strip() for path in changed_files if path.strip()})
    if not normalized:
        return TestPlan("full", (), "无法获得 PR 变更清单，按失败关闭策略运行完整回归")

    if _is_signal_fast_path(normalized):
        return TestPlan("targeted", SIGNAL_FAST_TEST_TARGETS, "信号公共层变更，运行审计过的共享层与消费者单测")

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
    services: set[str] = set()
    for path in normalized:
        if _is_unit_test(path):
            service = path.split("/", 2)[1] if path.startswith("backend/") else None
            if service:
                service_target = _service_test_target(repo_root, path.replace("/tests/", "/app/", 1))
                if service_target is None:
                    return TestPlan("full", (), "服务单测目录不可安全确定，升级为完整回归")
                services.add(service)
                targets.add(service_target)
            else:
                targets.add(path)
            continue

        if path.startswith("backend/") and path.endswith(".py"):
            service_target = _service_test_target(repo_root, path)
            if service_target is None:
                return TestPlan("full", (), "无法将后端源码安全归属到服务单元测试目录")
            services.add(path.split("/", 2)[1])
            targets.add(service_target)

    if len(services) > MAX_FAST_SERVICES:
        return TestPlan("full", (), f"受影响服务超过 {MAX_FAST_SERVICES} 个，升级为完整回归")

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
