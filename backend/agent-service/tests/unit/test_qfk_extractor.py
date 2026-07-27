from unittest.mock import AsyncMock, MagicMock

import pytest
from app.tools.acli.executor import ExecResult
from app.tools.qfk.extractor import (
    QFKExtractionError,
    extract_text_value,
    get_complete_output,
)


def _result(**overrides) -> ExecResult:
    values = {
        "stdout": "short stdout",
        "stderr": "short stderr",
        "exit_code": 0,
        "command": "acli system ps auxf",
        "node": "172.28.24.2",
        "duration_ms": 10,
        "truncated": False,
        "risk_level": 1,
        "exec_id": "exec-1",
    }
    values.update(overrides)
    return ExecResult(**values)


def test_ps_auxf_grep_awk_equivalent():
    output = """USER PID COMMAND
root 31315 /usr/libexec/qemu-kvm -id 8243094091404
root 31399 grep -id 8243094091404
"""
    value = extract_text_value(
        output,
        {
            "type": "text",
            "include": ["-id 8243094091404"],
            "exclude": ["grep"],
            "column": 2,
            "column_mode": "index",
        },
        "integer",
    )
    assert value == 31315


def test_multiple_include_defaults_to_and_and_supports_case_insensitive():
    output = "alpha TARGET one\nalpha other\n"
    assert extract_text_value(
        output,
        {"type": "text", "include": ["ALPHA", "target"], "case_sensitive": False},
    ) == "alpha TARGET one"


def test_exclude_and_first_last_all_cardinality():
    output = "keep 1\nkeep 2\nkeep debug 3\n"
    base = {"type": "text", "include": ["keep"], "exclude": ["debug"], "column": 2}
    assert extract_text_value(output, {**base, "cardinality": "first"}, "integer") == 1
    assert extract_text_value(output, {**base, "cardinality": "last"}, "integer") == 2
    assert extract_text_value(output, {**base, "cardinality": "all"}, "integer") == [1, 2]
    assert extract_text_value(output, {**base, "cardinality": "all"}, "array") == ["1", "2"]


def test_whole_line_and_from_index():
    output = "root 42 qemu command with spaces\n"
    assert extract_text_value(output, {"type": "text"}) == output.strip()
    assert extract_text_value(
        output,
        {"type": "text", "column": 3, "column_mode": "from_index"},
    ) == "qemu command with spaces"


def test_custom_delimiter_and_scalar_casts():
    assert extract_text_value("name:3.5:true", {"type": "text", "delimiter": ":", "column": 2}, "number") == 3.5
    assert extract_text_value("name:3.5:true", {"type": "text", "delimiter": ":", "column": 3}, "boolean") is True


@pytest.mark.parametrize(
    ("output", "spec", "code"),
    [
        ("", {"type": "text"}, "QFK_OUTPUT_EMPTY"),
        ("alpha\n", {"type": "text", "include": ["missing"]}, "QFK_NO_MATCH"),
        ("a\na\n", {"type": "text", "include": ["a"]}, "QFK_MULTIPLE_MATCHES"),
        ("one two\n", {"type": "text", "column": 3}, "QFK_COLUMN_OUT_OF_RANGE"),
        ("not-int\n", {"type": "text"}, "QFK_TYPE_CAST_FAILED"),
    ],
)
def test_fail_closed_errors(output, spec, code):
    value_type = "integer" if code == "QFK_TYPE_CAST_FAILED" else "string"
    with pytest.raises(QFKExtractionError) as exc:
        extract_text_value(output, spec, value_type)
    assert exc.value.code == code


def test_malicious_text_is_data_not_code():
    assert extract_text_value("root 7 $(rm -rf /)\n", {"type": "text", "column": 2}, "integer") == 7


@pytest.mark.asyncio
async def test_complete_stdout_without_truncation_does_not_read_cache():
    redis = MagicMock()
    redis.client.get = AsyncMock()
    assert await get_complete_output(_result(), redis) == "short stdout"
    redis.client.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_complete_stdout_and_stderr_use_separate_cache_keys():
    redis = MagicMock()
    redis.client.get = AsyncMock(side_effect=[b"full stdout", "full stderr"])
    result = _result(truncated=True, stderr_truncated=True)
    assert await get_complete_output(result, redis, source="stdout") == "full stdout"
    assert await get_complete_output(result, redis, source="stderr") == "full stderr"
    assert redis.client.get.await_args_list[0].args == ("cmd_cache:exec-1",)
    assert redis.client.get.await_args_list[1].args == ("cmd_stderr_cache:exec-1",)


@pytest.mark.asyncio
async def test_truncated_cache_miss_and_output_too_large_fail_closed():
    redis = MagicMock()
    redis.client.get = AsyncMock(return_value=None)
    with pytest.raises(QFKExtractionError) as exc:
        await get_complete_output(_result(truncated=True), redis)
    assert exc.value.code == "QFK_OUTPUT_TRUNCATED_SOURCE_UNAVAILABLE"

    with pytest.raises(QFKExtractionError) as exc:
        await get_complete_output(_result(stdout="abcd"), redis, max_bytes=3)
    assert exc.value.code == "QFK_OUTPUT_TOO_LARGE"


@pytest.mark.asyncio
async def test_truncated_cache_read_error_fails_closed():
    redis = MagicMock()
    redis.client.get = AsyncMock(side_effect=ConnectionError("redis unavailable"))
    with pytest.raises(QFKExtractionError) as exc:
        await get_complete_output(_result(truncated=True), redis)
    assert exc.value.code == "QFK_OUTPUT_TRUNCATED_SOURCE_UNAVAILABLE"
