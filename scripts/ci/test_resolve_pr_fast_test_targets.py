"""覆盖 PR 快速测试范围解析的关键判定与失败关闭边界。"""

from __future__ import annotations

from pathlib import Path

import pytest
from resolve_pr_fast_test_targets import (
    SERVICE_TEST_SCOPES,
    SIGNAL_FAST_TEST_TARGETS,
    resolve_test_plan,
)


@pytest.fixture()
def repo_root(tmp_path: Path) -> Path:
    """构造与 ci.yml 完整路径一致的最小目录树。"""
    for scope in SERVICE_TEST_SCOPES.values():
        (tmp_path / scope).mkdir(parents=True, exist_ok=True)
    (tmp_path / "tests" / "unit").mkdir(parents=True, exist_ok=True)
    return tmp_path


# ── 范围收敛：只运行受影响服务的范围 ──────────────────────────────


def test_reported_multi_service_pr_runs_only_affected_scopes(repo_root: Path) -> None:
    """PR #1052 的真实变更清单：修复前被判 full（实测 229s），实际只需两个服务范围。"""
    changed = [
        ".github/workflows/ci.yml",
        "backend/agent-service/app/adapters/agents/htp/investigation_agent.py",
        "backend/conversation-service/app/services/conversation_service.py",
        "backend/conversation-service/tests/unit/test_semantic_entry_metadata.py",
        "docs/README.md",
        "scripts/ci/resolve_main_test_scope.py",
    ]
    plan = resolve_test_plan(changed, repo_root)
    assert plan.mode == "targeted"
    assert plan.targets == (
        "backend/agent-service/tests/unit",
        "backend/conversation-service/tests/unit",
    )


def test_three_services_stay_targeted(repo_root: Path) -> None:
    changed = [
        "backend/agent-service/app/main.py",
        "backend/conversation-service/app/main.py",
        "backend/case-service/app/main.py",
    ]
    assert resolve_test_plan(changed, repo_root).mode == "targeted"


def test_four_services_fall_back_to_full(repo_root: Path) -> None:
    changed = [
        "backend/agent-service/app/main.py",
        "backend/conversation-service/app/main.py",
        "backend/case-service/app/main.py",
        "backend/scheduler-service/app/main.py",
    ]
    assert resolve_test_plan(changed, repo_root).mode == "full"


def test_kb_service_maps_to_whole_tests_dir(repo_root: Path) -> None:
    """kb-service 的 tests/unit 下没有测试文件，必须映射到完整路径使用的 tests/。"""
    plan = resolve_test_plan(["backend/kb-service/app/routes/admin.py"], repo_root)
    assert plan.mode == "targeted"
    assert plan.targets == ("backend/kb-service/tests",)


def test_root_unit_test_targets_the_file_itself(repo_root: Path) -> None:
    plan = resolve_test_plan(["tests/unit/test_foo.py"], repo_root)
    assert plan.targets == ("tests/unit/test_foo.py",)


# ── 非测试输入：不改变 pytest 输入的组织类路径 ────────────────────


def test_ci_infra_only_change_skips_unit_tests(repo_root: Path) -> None:
    changed = [
        ".github/workflows/ci.yml",
        "scripts/ci/check_docs_naming.py",
        "docs/README.md",
        "README.md",
    ]
    assert resolve_test_plan(changed, repo_root).mode == "none"


def test_frontend_only_change_skips_unit_tests(repo_root: Path) -> None:
    assert resolve_test_plan(["frontend/admin/src/App.vue"], repo_root).mode == "none"


def test_ci_infra_does_not_upgrade_a_service_change(repo_root: Path) -> None:
    """工作流改动不是 pytest 输入，不应把它和同批后端改动一起升级为完整回归。"""
    changed = [".github/workflows/ci.yml", "backend/case-service/app/main.py"]
    plan = resolve_test_plan(changed, repo_root)
    assert plan.mode == "targeted"
    assert plan.targets == ("backend/case-service/tests/unit",)


# ── 失败关闭边界 ──────────────────────────────────────────────────


def test_empty_change_list_falls_back_to_full(repo_root: Path) -> None:
    assert resolve_test_plan([], repo_root).mode == "full"


def test_blank_only_change_list_falls_back_to_full(repo_root: Path) -> None:
    assert resolve_test_plan(["", "   "], repo_root).mode == "full"


def test_shared_dependency_falls_back_to_full(repo_root: Path) -> None:
    assert resolve_test_plan(["backend/shared/schemas/signal_schema.py"], repo_root).mode == "full"


def test_database_schema_change_falls_back_to_full(repo_root: Path) -> None:
    """schema/迁移是全服务共享输入：此前被判 none，job 变绿却零验证。"""
    plan = resolve_test_plan(["database/atlas-migrations/20260916000001_add_x.sql"], repo_root)
    assert plan.mode == "full"
    assert plan.targets == ()


def test_database_seed_change_falls_back_to_full(repo_root: Path) -> None:
    """样例种子被 contract-smoke 与样例契约测试直读，同样是测试输入。"""
    assert resolve_test_plan(["database/seeds/04_kbd_diagnosis_samples.sql"], repo_root).mode == "full"


def test_database_change_alongside_service_stays_full(repo_root: Path) -> None:
    changed = ["database/desired_schema.sql", "backend/case-service/app/main.py"]
    assert resolve_test_plan(changed, repo_root).mode == "full"


def test_resolver_self_change_falls_back_to_full(repo_root: Path) -> None:
    """快速路径是经过审计的资产，改动它必须重新验证完整回归。"""
    path = "scripts/ci/resolve_pr_fast_test_targets.py"
    assert resolve_test_plan([path], repo_root).mode == "full"


def test_imported_script_change_falls_back_to_full(repo_root: Path) -> None:
    """kb-service 单测直接从该脚本导入被测函数，它属于测试输入。"""
    assert resolve_test_plan(["scripts/evaluate_multi_agent_extraction.py"], repo_root).mode == "full"


def test_pyproject_change_falls_back_to_full(repo_root: Path) -> None:
    assert resolve_test_plan(["pyproject.toml"], repo_root).mode == "full"


def test_unknown_service_falls_back_to_full(repo_root: Path) -> None:
    assert resolve_test_plan(["backend/new-service/app/main.py"], repo_root).mode == "full"


def test_missing_scope_dir_falls_back_to_full(tmp_path: Path) -> None:
    (tmp_path / "backend" / "case-service" / "tests" / "unit").mkdir(parents=True)
    assert resolve_test_plan(["backend/agent-service/app/main.py"], tmp_path).mode == "full"


def test_audited_signal_fast_path_survives_shared_prefix(repo_root: Path) -> None:
    """已审计的信号快速路径粒度更细，不能被 backend/shared/ 前缀判定吞掉。"""
    plan = resolve_test_plan(["backend/shared/signals/models.py"], repo_root)
    assert plan.mode == "targeted"
    assert plan.targets == SIGNAL_FAST_TEST_TARGETS


def test_signal_change_with_unknown_shared_file_falls_back_to_full(repo_root: Path) -> None:
    """信号白名单混入白名单外的共享代码时，快速路径失效，必须完整回归。"""
    changed = [
        "backend/shared/signals/ai_extractor.py",
        "backend/shared/observability/otel.py",
    ]
    assert resolve_test_plan(changed, repo_root).mode == "full"


def test_too_many_direct_test_targets_falls_back_to_full(repo_root: Path) -> None:
    """直接改动的散装测试文件数超过 MAX_FAST_TARGETS 时，收集成本高于完整回归。

    服务内的测试改动按目录收敛，只有根 tests/ 下的测试文件逐个计为目标，
    因此这里必须用根目录测试才能触达上限。
    """
    changed = [f"tests/unit/test_case_{i}.py" for i in range(9)]
    plan = resolve_test_plan(changed, repo_root)
    assert plan.mode == "full"
    assert plan.targets == ()


# ── 映射与仓库现状的一致性守护 ────────────────────────────────────


def test_mapped_scopes_are_used_by_the_full_ci_path() -> None:
    """映射必须指向 ci.yml 完整路径实际运行的范围，否则快速路径与完整路径语义不一致。"""
    repo_root = Path(__file__).resolve().parents[2]
    ci_config = (repo_root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    for scope in SERVICE_TEST_SCOPES.values():
        assert f"{scope}/" in ci_config, scope


def test_mapped_scopes_contain_real_tests() -> None:
    """映射到的目录必须真的有测试用例，否则快速路径会收集 0 个用例并让 PR 假失败。"""
    repo_root = Path(__file__).resolve().parents[2]
    for scope in SERVICE_TEST_SCOPES.values():
        assert list((repo_root / scope).rglob("test_*.py")), scope
