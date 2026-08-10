from kbd.pipeline import Stage
from kbd.task_manager import (
    TaskMode,
    TaskState,
    parse_requested_stage_names,
    parse_rework_statuses,
    parse_stage_names,
    parse_task_mode,
    select_task_ids,
    select_task_plan,
)


def test_modes_select_exact_lifecycle_bucket():
    states = {
        ("1", Stage.FETCH): TaskState(),
        ("2", Stage.FETCH): TaskState(executed=True, success=True),
        ("3", Stage.FETCH): TaskState(executed=True, success=False),
    }
    assert select_task_ids(["1", "2", "3"], stage=Stage.FETCH, states=states, mode=TaskMode.DEFAULT) == ["1", "3"]
    assert select_task_ids(["1", "2", "3"], stage=Stage.FETCH, states=states, mode=TaskMode.RESUME) == ["1"]
    assert select_task_ids(["1", "2", "3"], stage=Stage.FETCH, states=states, mode=TaskMode.FAILED) == ["3"]
    assert select_task_ids(["1", "2", "3"], stage=Stage.FETCH, states=states, mode=TaskMode.REWORK) == ["1", "2", "3"]


def test_modes_are_mutually_exclusive():
    assert parse_task_mode(resume=False, failed=False, rework=False) is TaskMode.DEFAULT
    for kwargs in (
        {"resume": True, "failed": True, "rework": False},
        {"resume": True, "failed": False, "rework": True},
        {"resume": False, "failed": True, "rework": True},
    ):
        try:
            parse_task_mode(**kwargs)
        except ValueError as exc:
            assert "只能选择一个" in str(exc)
        else:
            raise AssertionError("组合模式必须在参数校验阶段失败")


def test_rework_status_defaults_and_validation():
    assert parse_rework_statuses(None) == ("draft",)
    assert parse_rework_statuses("draft,published,draft") == ("draft", "published")
    assert parse_rework_statuses("draft,published,rejected,archived") == (
        "draft", "published", "rejected", "archived"
    )
    try:
        parse_rework_statuses("pending")
    except ValueError as exc:
        assert "状态非法" in str(exc)
    else:
        raise AssertionError("非法 KBD 状态必须失败")


def test_stage_selection_expands_dependencies_and_defaults_to_all():
    assert parse_stage_names(None) == tuple(Stage)
    assert parse_stage_names("vision") == (Stage.FETCH, Stage.IMPORT, Stage.VISION)
    assert parse_stage_names("extract-signals") == (
        Stage.FETCH,
        Stage.IMPORT,
        Stage.CLASSIFY,
        Stage.VISION,
        Stage.EXTRACT_SIGNALS,
    )


def test_rework_only_reworks_requested_stage_when_dependencies_succeeded():
    ids = ["27123"]
    requested = parse_requested_stage_names("extract-signals")
    resolved = parse_stage_names("extract-signals")
    states = {
        ("27123", stage): TaskState(executed=True, success=True)
        for stage in resolved
    }

    plan = select_task_plan(
        ids,
        requested_stages=requested,
        resolved_stages=resolved,
        states=states,
        mode=TaskMode.REWORK,
    )

    assert plan[Stage.EXTRACT_SIGNALS] == ids
    assert all(not plan[stage] for stage in resolved if stage is not Stage.EXTRACT_SIGNALS)


def test_rework_adds_only_blocking_dependency_chain():
    ids = ["27123"]
    requested = parse_requested_stage_names("extract-signals")
    resolved = parse_stage_names("extract-signals")
    states = {
        ("27123", Stage.FETCH): TaskState(executed=True, success=True),
        ("27123", Stage.IMPORT): TaskState(executed=True, success=True),
        ("27123", Stage.CLASSIFY): TaskState(executed=True, success=True),
        ("27123", Stage.VISION): TaskState(executed=True, success=False),
    }

    plan = select_task_plan(
        ids,
        requested_stages=requested,
        resolved_stages=resolved,
        states=states,
        mode=TaskMode.REWORK,
    )

    assert plan[Stage.EXTRACT_SIGNALS] == ids
    assert plan[Stage.VISION] == ids
    assert all(not plan[stage] for stage in (Stage.FETCH, Stage.IMPORT, Stage.CLASSIFY))
