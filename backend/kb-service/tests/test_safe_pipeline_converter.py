import pytest
from app.services.safe_pipeline_converter import (
    SafePipelineConversionError,
    apply_safe_pipeline_to_signal,
    convert_safe_pipeline,
)


def test_converts_real_ps_grep_awk_command():
    result = convert_safe_pipeline(
        "acli system ps auxf | grep -e '-id 8243094091404' | grep -v grep | awk '{print $2}'"
    )
    assert result.command == "ps auxf"
    assert result.extract == {
        "type": "text",
        "include": ["-id 8243094091404"],
        "column": 2,
        "column_mode": "index",
    }
    assert result.removed_segments


def test_grep_flags_and_multiple_grep_become_structured_filters():
    result = convert_safe_pipeline("ps auxf | grep -Fi VM | grep -v debug | awk '{ print $3; }'")
    assert result.extract["include"] == ["VM"]
    assert result.extract["exclude"] == ["debug"]
    assert result.extract["case_sensitive"] is False
    assert result.extract["column"] == 3


def test_cut_single_character_delimiter():
    result = convert_safe_pipeline("getent passwd | grep -F root | cut -d: -f3")
    assert result.command == "getent passwd"
    assert result.extract["delimiter"] == ":"
    assert result.extract["column"] == 3


def test_placeholders_and_quotes_are_preserved_as_data():
    result = convert_safe_pipeline("ps auxf | grep -e '-id {{VM}}' | awk '{print $2}'")
    assert result.extract["include"] == ["-id {{VM}}"]


@pytest.mark.parametrize(
    "command",
    [
        "ps auxf | sed -n 1p",
        "ps auxf | awk '{sum += $2} END {print sum}'",
        "ps auxf | grep 'vm.*qemu'",
        "ps auxf | sort",
        "ps auxf || echo failed",
    ],
)
def test_complex_or_unknown_pipeline_fails_closed(command):
    with pytest.raises(SafePipelineConversionError):
        convert_safe_pipeline(command)


def test_apply_converter_requires_one_explicit_output_variable():
    signal = {
        "acquire": {"tool": "qfk_system", "args": {"command": "ps auxf | grep VM"}},
        "match": None,
        "orchestrate": {"produces": []},
    }
    with pytest.raises(SafePipelineConversionError) as exc:
        apply_safe_pipeline_to_signal(signal)
    assert exc.value.code == "QFK_PIPELINE_OUTPUT_VARIABLE_REQUIRED"


def test_apply_converter_updates_signal_without_executing_input():
    signal = {
        "acquire": {"tool": "qfk_system", "args": {"command": "ps auxf | grep -e '-id {{VM}}' | awk '{print $2}'"}},
        "match": None,
        "orchestrate": {"produces": [{"name": "KVM_PID", "type": "integer", "path": ""}]},
    }
    assert apply_safe_pipeline_to_signal(signal) is True
    assert signal["acquire"]["args"]["command"] == "ps auxf"
    assert signal["orchestrate"]["produces"][0]["extract"]["column"] == 2
    assert "path" not in signal["orchestrate"]["produces"][0]
