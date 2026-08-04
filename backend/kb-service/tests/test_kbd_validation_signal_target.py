from collections import deque

import jsonschema
from app.routes.admin import _humanize_signal_validation_error


def test_schema_validation_issue_targets_stable_signal_id_not_array_position():
    error = jsonschema.ValidationError(
        "'any' is not one of ['or', 'and', 'not']",
        path=deque(["signals", 1, "match", "mode"]),
    )
    issue = _humanize_signal_validation_error(
        error,
        [
            {"id": "sig_frontend"},
            {"id": "sig_backend"},
        ],
    )

    assert issue["location"] == "关键信号 · sig_backend · 判定器"
    assert issue["field_path"] == "match.mode"
    assert issue["action"] == {
        "type": "edit_signal",
        "signal_id": "sig_backend",
        "focus": "match.mode",
    }
