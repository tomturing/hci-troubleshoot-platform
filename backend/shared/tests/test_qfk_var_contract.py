"""qfk_var acquire 与 Signal 专属门禁测试。"""

import pytest
from jsonschema import ValidationError
from shared.schemas.acquirer_args import validate_acquire_args
from shared.schemas.signal_schema import validate_signals_json


def _args(mode: str = "assert") -> dict:
    if mode == "assert":
        return {
            "schema_version": 1,
            "mode": "assert",
            "operation": "compare",
            "left": "91%",
            "right": "90%",
            "operator": ">",
            "value_type": "percentage",
        }
    return {
        "schema_version": 1,
        "mode": "derive",
        "operation": "cast",
        "input": "{{DESCRIPTION}}",
        "value_type": "string",
    }


def test_qfk_var_assert_and_derive_args_are_valid() -> None:
    assert validate_acquire_args("qfk_var", _args())[0]
    assert validate_acquire_args("qfk_var", _args("derive"))[0]
    assert validate_acquire_args("qfk_var", {**_args("derive"), "on_error": "unknown"})[0]


def test_qfk_var_target_type_is_fixed() -> None:
    args = {
        "schema_version": 1,
        "mode": "derive",
        "operation": "feature_extract",
        "input": "{{DESCRIPTION}}",
        "target_variable": "percent.current",
        "value_type": "number",
        "cardinality": "exactly_one",
    }
    ok, error = validate_acquire_args("qfk_var", args)
    assert not ok and "固定类型" in str(error)


def test_qfk_var_signal_requires_null_match_and_single_produce() -> None:
    derive = {
        "schema_version": 2,
        "signals": [
            {
                "id": "sig_var",
                "acquire": {
                    "tool": "qfk_var",
                    "args": {
                        "schema_version": 1,
                        "mode": "derive",
                        "operation": "cast",
                        "input": "{{VALUE}}",
                        "value_type": "string",
                    },
                },
                "match": None,
                "orchestrate": {"produces": [{"name": "DERIVED", "type": "string"}], "requires": ["VALUE"]},
            }
        ],
    }
    validate_signals_json(derive)

    invalid = {**derive, "signals": [{**derive["signals"][0], "match": {"type": "exists", "expected": True}}]}
    with pytest.raises(ValidationError):
        validate_signals_json(invalid)
