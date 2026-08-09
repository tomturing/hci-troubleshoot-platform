import json

from kbd import task_state


def test_manifest_is_immutable_and_run_id_is_a_historical_scope(tmp_path, monkeypatch):
    monkeypatch.setattr(task_state.settings, "KBD_LOGS_DIR", tmp_path)
    manifest = {
        "execution_id": "20260809_120000",
        "source_run_id": None,
        "requested_ids": ["29351"],
        "resolved_stages": ["fetch", "vision"],
        "selected_tasks": {"fetch": ["29351"], "vision": ["29351"]},
    }
    task_state.save_execution_manifest(manifest)
    assert task_state.load_execution_manifest("20260809_120000") == manifest
    task_state.save_execution_manifest(manifest)
    altered = dict(manifest, requested_ids=["29352"])
    try:
        task_state.save_execution_manifest(altered)
    except ValueError as exc:
        assert "不同的任务 manifest" in str(exc)
    else:
        raise AssertionError("manifest 必须不可变")


def test_state_save_uses_atomic_json_and_round_trips(tmp_path, monkeypatch):
    monkeypatch.setattr(task_state.settings, "KBD_LOGS_DIR", tmp_path)
    from kbd.pipeline import Stage
    from kbd.task_manager import TaskState

    states = {("29351", Stage.VISION): TaskState(executed=True, success=False, rework=True)}
    task_state.save_state(states)
    raw = json.loads((tmp_path / "task-state.json").read_text())
    assert raw["version"] == 1
    assert task_state.load_state()[("29351", Stage.VISION)].failed
