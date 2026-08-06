"""QKV/QFK acquire 参数类型边界回归。"""

from shared.schemas.acquirer_args import (
    QKV_KEYWORD_TYPE_ERROR_CODE,
    validate_acquire_args,
)


def test_qkv_keyword_is_a_single_string():
    ok, error = validate_acquire_args(
        "qkv_task",
        {"keyword": "迁移虚拟机", "is_failed": True, "limit": 1},
    )

    assert ok is True
    assert error is None


def test_qkv_keyword_array_is_rejected_with_actionable_code():
    ok, error = validate_acquire_args(
        "qkv_task",
        {"keyword": ["迁移虚拟机"], "is_failed": True, "limit": 1},
    )

    assert ok is False
    assert error is not None
    assert QKV_KEYWORD_TYPE_ERROR_CODE in error
    assert "match.pattern" in error
    assert "extract.rows.include/exclude" in error


def test_qkv_keyword_non_string_types_use_the_same_contract_code():
    ok, error = validate_acquire_args("qkv_alert", {"keyword": 123})

    assert ok is False
    assert error is not None
    assert error.startswith(f"{QKV_KEYWORD_TYPE_ERROR_CODE}:")
