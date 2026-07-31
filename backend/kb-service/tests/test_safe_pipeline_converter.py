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
        "rows": {"mode": "keywords", "include": ["-id 8243094091404"], "exclude": [], "include_mode": "all", "case_sensitive": True},
        "parser": "whitespace_table",
        "columns": [{"key": "COLUMN_2", "selector": {"by": "index", "index": 2}, "value_mode": "string"}],
        "value_key": "COLUMN_2",
    }
    assert result.removed_segments


def test_grep_flags_and_multiple_grep_become_structured_filters():
    result = convert_safe_pipeline("ps auxf | grep -Fi VM | grep -v debug | awk '{ print $3; }'")
    assert result.extract["rows"]["include"] == ["VM"]
    assert result.extract["rows"]["exclude"] == ["debug"]
    assert result.extract["rows"]["case_sensitive"] is False
    assert result.extract["columns"][0]["selector"]["index"] == 3


def test_cut_single_character_delimiter():
    result = convert_safe_pipeline("getent passwd | grep -F root | cut -d: -f3")
    assert result.command == "getent passwd"
    assert result.extract["delimiter"] == ":"
    assert result.extract["columns"][0]["selector"]["index"] == 3


def test_multi_column_awk_and_cut_are_converted_to_structured_columns():
    awk = convert_safe_pipeline("df -P /sf/log | awk '{print $3, $5}'")
    assert awk.extract == {
        "type": "text",
        "rows": {"mode": "all"},
        "parser": "whitespace_table",
        "columns": [
            {"key": "COLUMN_3", "selector": {"by": "index", "index": 3}, "value_mode": "string"},
            {"key": "COLUMN_5", "selector": {"by": "index", "index": 5}, "value_mode": "string"},
        ],
    }
    assert len(awk.conversion_id) == 64

    cut = convert_safe_pipeline("getent passwd | cut -d: -f3,5")
    assert cut.extract["parser"] == "delimited_table"
    assert cut.extract["delimiter"] == ":"
    assert [item["selector"]["index"] for item in cut.extract["columns"]] == [3, 5]


def test_sed_and_awk_nr_preserve_physical_line_basis():
    sed = convert_safe_pipeline("df -P /sf/log | sed -n '2,3p'")
    assert sed.extract == {
        "type": "text",
        "rows": {"mode": "indices", "basis": "physical", "ranges": [{"start": 2, "end": 3}]},
    }

    awk = convert_safe_pipeline("df -P /sf/log | awk 'NR==2 {print $5}'")
    assert awk.extract["rows"] == {"mode": "indices", "basis": "physical", "indices": [2]}
    assert awk.extract["value_key"] == "COLUMN_5"


def test_placeholders_and_quotes_are_preserved_as_data():
    result = convert_safe_pipeline("ps auxf | grep -e '-id {{VM}}' | awk '{print $2}'")
    assert result.extract["rows"]["include"] == ["-id {{VM}}"]


@pytest.mark.parametrize(
    "command",
    [
        "ps auxf | sed -n '0p'",
        "ps auxf | sed -n '2p' | grep VM",
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
        "orchestrate": {"produces": [{"name": "KVM_PID", "type": "integer", "extract": {"type": "text", "rows": {"mode": "all"}}}]},
    }
    assert apply_safe_pipeline_to_signal(signal) is True
    assert signal["acquire"]["args"]["command"] == "ps auxf"
    assert signal["orchestrate"]["produces"][0]["extract"]["columns"][0]["selector"]["index"] == 2


def test_apply_converter_preserves_match_mode_and_predicate():
    signal = {
        "acquire": {"tool": "qfk_system", "args": {"command": "df -P /sf/log | awk '{print $5}'"}},
        "match": {"type": "threshold", "aggregation": "max", "operator": ">", "value": 80, "expected": True, "extract": {"type": "text", "rows": {"mode": "all"}}},
        "orchestrate": {"produces": []},
    }

    assert apply_safe_pipeline_to_signal(signal) is True
    assert signal["acquire"]["args"]["command"] == "df -P /sf/log"
    assert signal["match"]["type"] == "threshold"
    assert signal["match"]["value"] == 80
    assert signal["match"]["extract"]["columns"][0]["selector"]["index"] == 5
    assert signal["orchestrate"]["produces"] == []
