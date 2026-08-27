from pathlib import Path

from scripts.ci.resolve_pr_fast_test_targets import resolve_test_plan


def test_changed_unit_test_is_selected_directly(tmp_path: Path) -> None:
    (tmp_path / "backend/agent-service/tests/unit").mkdir(parents=True)
    plan = resolve_test_plan(["backend/agent-service/tests/unit/test_signal_dry_run.py"], tmp_path)

    assert plan.mode == "targeted"
    assert plan.targets == ("backend/agent-service/tests/unit",)


def test_source_change_selects_service_test_directory(tmp_path: Path) -> None:
    (tmp_path / "backend/agent-service/tests/unit").mkdir(parents=True)

    plan = resolve_test_plan(["backend/agent-service/app/routes/signal_dry_run.py"], tmp_path)

    assert plan.mode == "targeted"
    assert plan.targets == ("backend/agent-service/tests/unit",)


def test_source_path_rename_does_not_require_test_rename(tmp_path: Path) -> None:
    (tmp_path / "backend/agent-service/tests/unit").mkdir(parents=True)

    plan = resolve_test_plan(["backend/agent-service/app/new_area/renamed_extractor.py"], tmp_path)

    assert plan.mode == "targeted"
    assert plan.targets == ("backend/agent-service/tests/unit",)


def test_multiple_services_fall_back_to_full(tmp_path: Path) -> None:
    for service in ("agent-service", "case-service"):
        (tmp_path / f"backend/{service}/tests/unit").mkdir(parents=True)

    plan = resolve_test_plan(
        [
            "backend/agent-service/app/routes/agent.py",
            "backend/case-service/app/routes/case.py",
        ],
        tmp_path,
    )

    assert plan.mode == "full"


def test_backend_test_change_uses_service_directory(tmp_path: Path) -> None:
    (tmp_path / "backend/agent-service/tests/unit").mkdir(parents=True)

    plan = resolve_test_plan(["backend/agent-service/tests/unit/test_qfk_ai_extractor.py"], tmp_path)

    assert plan.mode == "targeted"
    assert plan.targets == ("backend/agent-service/tests/unit",)


def test_shared_code_change_fails_closed_to_full_regression(tmp_path: Path) -> None:
    plan = resolve_test_plan(["backend/shared/observability/otel.py"], tmp_path)

    assert plan.mode == "full"
    assert plan.targets == ()


def test_shared_signal_change_uses_audited_fast_suite(tmp_path: Path) -> None:
    plan = resolve_test_plan(["backend/shared/signals/ai_extractor.py"], tmp_path)

    assert plan.mode == "targeted"
    assert "backend/shared/tests/test_ai_extractor.py" in plan.targets
    assert "backend/agent-service/tests/unit/test_signal_dry_run.py" in plan.targets


def test_signal_change_with_unknown_shared_file_falls_back_to_full(tmp_path: Path) -> None:
    plan = resolve_test_plan(
        [
            "backend/shared/signals/ai_extractor.py",
            "backend/shared/observability/otel.py",
        ],
        tmp_path,
    )

    assert plan.mode == "full"


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
