#!/usr/bin/env python3
"""根据 PR 变更生成保守的 Python 快速测试计划。

第一性原理：一个 job 的每一步都只为「输入在本次变更内」的测试付费。PR 上的
unit-tests 因此只应运行受影响服务的测试范围，而不是 8 个服务的完整回归
（实测完整路径 143s，其中 kb-service 的 58s 已查明为覆盖率插桩放大，见 ci.yml
注释；而只改 2 个服务时真正需要的证据约 25s）。

判定采用「白名单 + 失败关闭」：
  - 只有当每个变更路径都能被安全归属时才走快速路径；
  - 共享依赖、无法归属的后端路径、服务数或目标数超限，一律回退完整回归。

四处刻意的设计：

1. 服务到测试范围的映射必须与 ci.yml「运行完整单元测试（含覆盖率）」中该服务
   的实际调用路径逐条一致。kb-service 的完整范围是 `tests/`（`tests/unit/`
   下没有任何测试文件），把它映射成 `tests/unit/` 会让快速路径收集到 0 个用例,
   pytest 以退出码 5 结束，PR 假失败且真实测试从未运行。
2. 工作流与 CI 治理脚本只决定「怎么跑 CI」，不决定「跑出什么」，不是 pytest 的
   输入，因此既不参与计划也不触发完整回归。安全网在主干：这些路径不在
   resolve_main_test_scope.py 的白名单内，main push 仍会运行完整后端门禁。
3. 全局依赖判定作用于完整变更清单而不是过滤后的清单，因此本脚本自身、共享层与
   测试基础设施的变更一定会回退完整回归（快速路径是经过审计的资产，改动它必须
   重新验证完整回归）；已审计的信号公共层快速路径优先于通用前缀规则，避免被
   backend/shared/ 前缀吞掉。
4. `database/**` 是共享输入而不是「非代码」：schema、迁移与种子被 `make db-sync`
   应用到 PostgreSQL，被 integration-tests 与样例契约测试直接消费（diagnosis
   -service 单测直读 desired_schema.sql，kb/agent 样例测试直读
   database/seeds/04_kbd_diagnosis_samples.sql）。注意 `db-migration-test.yml`
   （DB Schema 声明式验证）虽已按 database/ 路径触发，但它只回答「DDL 能否应用」，
   既不验证服务与测试对新 schema 的兼容性，也不是必需检查（无法阻断合并）。不把
   它登记为全局依赖时，纯 schema PR 会被判为 none：ci.yml 侧一个用例都不跑，唯一
   证据来自一个非阻断 workflow——这是与 kb-service 空目录同类的静默漏测。
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path

MAX_FAST_SERVICES = 3
MAX_FAST_TARGETS = 8

# 与 ci.yml 完整路径逐条对齐：快速路径对某服务运行的范围必须覆盖完整路径对
# 同一服务运行的范围，否则会静默漏测。新增服务必须同时更新此处与 ci.yml。
SERVICE_TEST_SCOPES = {
    "api-gateway": "backend/api-gateway/tests/unit",
    "agent-service": "backend/agent-service/tests/unit",
    "case-service": "backend/case-service/tests/unit",
    "conversation-service": "backend/conversation-service/tests/unit",
    "kb-service": "backend/kb-service/tests",
    "diagnosis-service": "backend/diagnosis-service/tests/unit",
    "scheduler-service": "backend/scheduler-service/tests/unit",
}

GLOBAL_DEPENDENCY_PREFIXES = (
    "backend/shared/",
    "backend/conftest.py",
    "tests/conftest.py",
    # database/** 是全服务共享输入：schema/迁移/种子被 db-sync 与样例契约测试消费，
    # 而 DB Schema 声明式验证既不管消费方兼容性也不阻断合并，理由见 docstring 第 4 条。
    "database/",
)
GLOBAL_DEPENDENCY_FILES = {
    "Makefile",
    "conftest.py",
    "pyproject.toml",
    "uv.lock",
    "scripts/ci/resolve_pr_fast_test_targets.py",
    # kb-service 单测直接从这里导入被测函数，脚本本身就是测试输入。
    "scripts/evaluate_multi_agent_extraction.py",
}

# 只影响 CI 的组织方式、不影响任何测试输入的路径。
NON_TEST_INPUT_PREFIXES = (".github/", "docs/", "scripts/ci/")
NON_TEST_INPUT_FILES = frozenset({"README.md", "AGENTS.md", "CLAUDE.md"})

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
    """按服务返回与完整回归一致的范围，不依赖源码与测试文件的命名关系。"""
    parts = Path(changed_path).parts
    if len(parts) < 4 or parts[0] != "backend" or parts[2] != "app":
        return None

    scope = SERVICE_TEST_SCOPES.get(parts[1])
    if scope is None:
        # 未登记的服务无法确认其完整范围，交由调用方回退完整回归。
        return None
    if not (repo_root / scope).is_dir():
        return None
    return scope


def _is_non_test_input(path: str) -> bool:
    """判断路径是否只改变 CI 的组织方式，而不是任何测试的输入。"""
    if path in NON_TEST_INPUT_FILES:
        return True
    return path.startswith(NON_TEST_INPUT_PREFIXES)


def _is_global_dependency(path: str) -> bool:
    """判断路径是否可能影响任意服务的测试。"""
    if path in GLOBAL_DEPENDENCY_FILES:
        return True
    if path.endswith("/pyproject.toml") or path.endswith("/uv.lock"):
        return True
    return path.startswith(GLOBAL_DEPENDENCY_PREFIXES)


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

    # 只改变 CI 组织方式的路径不参与计划：它们不是 pytest 的输入。
    code_paths = [path for path in normalized if not _is_non_test_input(path)]

    # 已审计的信号公共层快速路径优先于通用规则：它逐条列举了变更文件，比
    # backend/shared/ 前缀判定的粒度更细，不能被前缀规则吞掉。
    if _is_signal_fast_path(code_paths):
        return TestPlan("targeted", SIGNAL_FAST_TEST_TARGETS, "信号公共层变更，运行审计过的共享层与消费者单测")

    # 全局依赖判定作用于完整清单（含被非测试输入过滤掉的路径），因此本脚本
    # 自身、共享层与测试基础设施的变更即使落在 scripts/ci/ 内也会完整回归。
    if any(_is_global_dependency(path) for path in normalized):
        return TestPlan("full", (), "共享依赖或测试基础设施变更需要完整回归")

    if not code_paths:
        return TestPlan("none", (), "本次 PR 只改动工作流、CI 治理脚本或文档，不是 pytest 的输入")

    targets: set[str] = set()
    services: set[str] = set()
    for path in code_paths:
        if _is_unit_test(path):
            service = path.split("/", 2)[1] if path.startswith("backend/") else None
            if service:
                service_target = SERVICE_TEST_SCOPES.get(service)
                if service_target is None or not (repo_root / service_target).is_dir():
                    return TestPlan("full", (), "服务测试范围不可安全确定，升级为完整回归")
                services.add(service)
                targets.add(service_target)
            else:
                targets.add(path)
            continue

        if path.startswith("backend/") and path.endswith(".py"):
            service_target = _service_test_target(repo_root, path)
            if service_target is None:
                return TestPlan("full", (), "无法将后端源码安全归属到服务测试范围")
            services.add(path.split("/", 2)[1])
            targets.add(service_target)

    if len(services) > MAX_FAST_SERVICES:
        return TestPlan("full", (), f"受影响服务超过 {MAX_FAST_SERVICES} 个，升级为完整回归")

    if not targets:
        return TestPlan("none", (), "本次 PR 未修改 Python 测试或后端源码")
    if len(targets) > MAX_FAST_TARGETS:
        return TestPlan("full", (), f"受影响测试超过 {MAX_FAST_TARGETS} 个，升级为完整回归")

    return TestPlan("targeted", tuple(sorted(targets)), "仅运行变更直接覆盖的服务测试范围")


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
