"""QKV 输出后处理的确定性契约测试。"""

import pytest
from shared.signals.qkv_output_processing import (
    QKVProcessingError,
    apply_output_processing,
    validate_output_processing,
)


def test_feature_extract_and_compare_are_applied_per_record() -> None:
    result = apply_output_processing(
        [{"description": "虚拟机名称：vm-001，使用率：92%"}],
        [
            {
                "id": "vm",
                "mode": "derive",
                "input": "{{DESCRIPTION}}",
                "operation": "feature_extract",
                "target_variable": "VM_NAME",
                "feature": "vm_name",
            },
            {
                "id": "percent",
                "mode": "assert",
                "input": "{{DESCRIPTION}}",
                "operation": "compare",
                "value_type": "percentage",
                "operator": ">",
                "right": "90%",
            },
        ],
    )
    assert result.records[0]["vm_name"] == "vm-001"
    assert result.matched is True
    assert result.assertions[0].status == "PASS"


def test_multiple_records_do_not_silently_choose_first_value() -> None:
    with pytest.raises(QKVProcessingError, match="QKV_CARDINALITY_MISMATCH"):
        apply_output_processing(
            [{"description": "虚拟机名称：vm-001"}, {"description": "虚拟机名称：vm-002"}],
            [{
                "id": "vm",
                "mode": "derive",
                "scope": "single",
                "input": "{{DESCRIPTION}}",
                "operation": "feature_extract",
                "target_variable": "VM_NAME",
                "feature": "vm_name",
            }],
        )


def test_unknown_operation_and_script_field_fail_closed() -> None:
    with pytest.raises(QKVProcessingError):
        validate_output_processing([{"id": "x", "mode": "derive", "input": "{{DESCRIPTION}}", "operation": "eval"}])
    with pytest.raises(QKVProcessingError):
        validate_output_processing([{"id": "x", "mode": "derive", "input": "{{DESCRIPTION}}", "operation": "trim", "script": "x"}])


def test_json_path_supports_nested_array_indexes() -> None:
    result = apply_output_processing(
        [{"payload": {"items": [{"name": "vm-001"}]}}],
        [{
            "id": "name",
            "mode": "derive",
            "input": "{{PAYLOAD}}",
            "operation": "json_path",
            "path": "items.0.name",
            "target_variable": "VM_NAME",
        }],
    )
    assert result.records[0]["vm_name"] == "vm-001"


def test_compare_is_assert_only() -> None:
    with pytest.raises(QKVProcessingError, match="compare 仅支持 assert"):
        validate_output_processing([{
            "id": "percent",
            "mode": "derive",
            "input": "{{DESCRIPTION}}",
            "operation": "compare",
            "target_variable": "PERCENT",
            "value_type": "percentage",
            "operator": ">",
            "right": "90%",
        }])


def test_assert_failure_does_not_create_derived_variable() -> None:
    result = apply_output_processing(
        [{"description": "使用率：80%"}],
        [{
            "id": "percent",
            "mode": "assert",
            "input": "{{DESCRIPTION}}",
            "operation": "compare",
            "value_type": "percentage",
            "operator": ">",
            "right": "90%",
        }],
    )
    assert result.matched is False
    assert "derived_value" not in result.records[0]
