from pathlib import Path

from scripts.ci.resolve_pr_fast_test_targets import resolve_test_plan


def test_changed_unit_test_is_selected_directly(tmp_path: Path) -> None:
    plan = resolve_test_plan(["backend/agent-service/tests/unit/test_signal_dry_run.py"], tmp_path)

    assert plan.mode == "targeted"
    assert plan.targets == ("backend/agent-service/tests/unit/test_signal_dry_run.py",)


def test_source_change_uses_same_name_service_test(tmp_path: Path) -> None:
    test_path = tmp_path / "backend/agent-service/tests/unit/test_signal_dry_run.py"
    test_path.parent.mkdir(parents=True)
    test_path.touch()

    plan = resolve_test_plan(["backend/agent-service/app/routes/signal_dry_run.py"], tmp_path)

    assert plan.mode == "targeted"
    assert plan.targets == ("backend/agent-service/tests/unit/test_signal_dry_run.py",)


def test_shared_code_change_fails_closed_to_full_regression(tmp_path: Path) -> None:
    plan = resolve_test_plan(["backend/shared/observability/otel.py"], tmp_path)

    assert plan.mode == "full"
    assert plan.targets == ()


def test_test_infrastructure_change_fails_closed_to_full_regression(tmp_path: Path) -> None:
    plan = resolve_test_plan(["backend/agent-service/pyproject.toml", "Makefile"], tmp_path)

    assert plan.mode == "full"
    assert plan.targets == ()


def test_too_many_direct_test_targets_falls_back_to_full_regression(tmp_path: Path) -> None:
    plan = resolve_test_plan(
        [
            "backend/agent-service/tests/unit/test_first.py",
            "backend/agent-service/tests/unit/test_second.py",
            "backend/agent-service/tests/unit/test_third.py",
        ],
        tmp_path,
    )

    assert plan.mode == "full"
    assert plan.targets == ()


def test_docs_only_change_does_not_start_python_test_runtime(tmp_path: Path) -> None:
    plan = resolve_test_plan(["docs/deploy/发布指南.md"], tmp_path)

    assert plan.mode == "none"
    assert plan.targets == ()
