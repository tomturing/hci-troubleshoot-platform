from unittest.mock import AsyncMock, MagicMock

import pytest
from app.tools.acli.executor import ExecResult
from app.tools.qfk.extractor import QFKExtractionError, extract_output_values, extract_value, get_complete_output

DF_OUTPUT = """Filesystem  Size  Used  Avail  Use%  Mounted on
tmpfs       512M   22M   491M    5%  /run
tmpfs       8.0M   12K   8.0M    1%  /run/lock
/dev/sda3   7.8G  2.6G   4.8G   35%  /
"""


def _df_extract(rows: dict) -> dict:
    return {
        "type": "text",
        "parser": "whitespace_table",
        "header": {"mode": "contains", "required": ["Filesystem", "Used", "Use%"], "case_sensitive": False},
        "rows": rows,
        "columns": [
            {"key": "USED", "selector": {"by": "header", "name": "Used"}, "value_mode": "string"},
            {"key": "USE_PERCENT", "selector": {"by": "header", "name": "Use%"}, "value_mode": "number"},
        ],
        "cardinality": "all",
        "value_key": "USE_PERCENT",
    }


def test_declarative_extract_selects_multiple_rows_and_columns():
    result = extract_output_values(
        DF_OUTPUT,
        _df_extract({"mode": "keywords", "include": ["tmpfs", "/dev/sda3"], "exclude": ["/run/lock"], "include_mode": "any", "case_sensitive": True}),
    )
    assert result.raw_records == [{"USED": "22M", "USE_PERCENT": "5%"}, {"USED": "2.6G", "USE_PERCENT": "35%"}]
    assert result.records == [{"USED": "22M", "USE_PERCENT": 5.0}, {"USED": "2.6G", "USE_PERCENT": 35.0}]
    assert result.selected_line_numbers == [2, 4]


def test_row_numbers_have_explicit_data_non_empty_and_physical_bases():
    spec = _df_extract({"mode": "indices", "basis": "data", "indices": [3]})
    assert extract_output_values(DF_OUTPUT, spec).values == [35.0]
    rows_only = {"type": "text", "rows": {"mode": "indices", "basis": "physical", "indices": [2]}, "cardinality": "exactly_one"}
    assert extract_value(DF_OUTPUT, rows_only) == "tmpfs       512M   22M   491M    5%  /run"


def test_header_aliases_are_explicit_and_capacity_units_do_not_silently_cast():
    spec = _df_extract({"mode": "indices", "basis": "data", "indices": [3]})
    spec["columns"][0]["selector"] = {"by": "header", "name": "Uesed", "aliases": []}
    with pytest.raises(QFKExtractionError, match="QFK_COLUMN_NOT_FOUND"):
        extract_output_values(DF_OUTPUT, spec)
    spec["columns"][0]["selector"]["aliases"] = ["Used"]
    assert extract_output_values(DF_OUTPUT, spec).records[0]["USED"] == "2.6G"
    spec["columns"] = [{"key": "USED", "selector": {"by": "header", "name": "Used"}, "value_mode": "number"}]
    spec["value_key"] = "USED"
    with pytest.raises(QFKExtractionError, match="QFK_TYPE_CAST_FAILED"):
        extract_output_values(DF_OUTPUT, spec)


def test_json_and_record_values_use_the_same_runtime_entry():
    result = extract_output_values('{"data":[{"status":"running","usage":"35%"}]}', {"type": "json", "path": "data[0].usage", "value_mode": "number"})
    assert result.values == [35.0]
    spec = _df_extract({"mode": "indices", "basis": "data", "indices": [3]})
    spec.pop("value_key")
    spec["cardinality"] = "exactly_one"
    assert extract_value(DF_OUTPUT, spec, "object") == {"USED": "2.6G", "USE_PERCENT": 35.0}


@pytest.mark.parametrize("spec", [{"type": "text"}, {"type": "text", "include": ["old"]}, {"type": "text", "rows": {"mode": "all"}, "column": 2}])
def test_old_or_incomplete_text_extract_is_rejected(spec):
    with pytest.raises(QFKExtractionError):
        extract_output_values("one two\n", spec)


def _result(**overrides) -> ExecResult:
    values = {"stdout": "short stdout", "stderr": "short stderr", "exit_code": 0, "command": "df", "node": "172.28.24.2", "duration_ms": 10, "truncated": False, "risk_level": 1, "exec_id": "exec-1"}
    values.update(overrides)
    return ExecResult(**values)


@pytest.mark.asyncio
async def test_complete_output_uses_separate_cache_and_fails_closed():
    redis = MagicMock()
    redis.client.get = AsyncMock(side_effect=[b"full stdout", "full stderr"])
    result = _result(truncated=True, stderr_truncated=True)
    assert await get_complete_output(result, redis, source="stdout") == "full stdout"
    assert await get_complete_output(result, redis, source="stderr") == "full stderr"
    redis.client.get = AsyncMock(return_value=None)
    with pytest.raises(QFKExtractionError, match="QFK_OUTPUT_TRUNCATED_SOURCE_UNAVAILABLE"):
        await get_complete_output(_result(truncated=True), redis)
