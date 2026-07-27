import pytest
from jsonschema import ValidationError
from shared.schemas.signal_output import derive_signal_requires, sync_signal_requires
from shared.schemas.signal_schema import validate_signals_json


def _doc(produce, *, tool="qfk_system", command="ps auxf"):
    return {
        "schema_version": 2,
        "signals": [
            {
                "acquire": {"tool": tool, "args": {"command": command} if tool == "qfk_system" else {"keyword": "失败"}},
                "match": None,
                "orchestrate": {"produces": [produce], "requires": []},
            }
        ],
    }


def test_old_json_path_and_empty_path_remain_compatible():
    validate_signals_json(_doc({"name": "PID", "type": "integer", "path": "data.0.pid"}))
    validate_signals_json(_doc({"name": "RAW", "path": ""}))


def test_text_extract_contract_is_accepted():
    validate_signals_json(
        _doc(
            {
                "name": "KVM_PID",
                "type": "integer",
                "extract": {
                    "type": "text",
                    "include": ["-id {{VM}}"],
                    "column": 2,
                    "column_mode": "index",
                },
            }
        )
    )


@pytest.mark.parametrize(
    "produce",
    [
        {"name": "PID", "path": "data.0.pid", "extract": {"type": "text"}},
        {"name": "PID", "extract": {"type": "text", "column": 0}},
        {"name": "PID", "extract": {"type": "text", "column_mode": "index"}},
        {"name": "PID", "extract": {"type": "text", "unknown": True}},
    ],
)
def test_invalid_extract_contract_is_rejected(produce):
    with pytest.raises(ValidationError):
        validate_signals_json(_doc(produce))


def test_qkv_text_extract_and_qfk_pipe_are_rejected():
    with pytest.raises(ValidationError, match="只支持 JSON path"):
        validate_signals_json(_doc({"name": "HOST", "extract": {"type": "text"}}, tool="qkv_task"))
    with pytest.raises(ValidationError, match="禁止保存 shell 管道"):
        validate_signals_json(_doc({"name": "PID", "path": ""}, command="ps auxf | grep VM"))


def test_requires_are_derived_from_args_and_extract_conditions():
    signal = _doc(
        {
            "name": "PID",
            "extract": {"type": "text", "include": ["-id {{VM}}", "{{TARGET}}"]},
        },
        command="ps auxf",
    )["signals"][0]
    signal["acquire"]["args"]["host"] = "{{HOST}}"
    assert derive_signal_requires(signal) == ["HOST", "TARGET", "VM"]
    assert sync_signal_requires(signal) == ["HOST", "TARGET", "VM"]
    assert signal["orchestrate"]["requires"] == ["HOST", "TARGET", "VM"]
