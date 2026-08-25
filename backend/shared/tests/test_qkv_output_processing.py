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
    with pytest.raises(QKVProcessingError, match="QFK_CARDINALITY_MISMATCH"):
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
    with pytest.raises(QKVProcessingError, match="QKV_PROCESSING_INVALID|QKV_PROCESSING_REDUNDANT_OPERATION"):
        validate_output_processing([{"id": "x", "mode": "derive", "input": "{{DESCRIPTION}}", "operation": "trim", "target_variable": "TEXT", "value_type": "boolean"}])


def test_json_path_is_rejected_because_qkv_produces_already_projects_values() -> None:
    with pytest.raises(QKVProcessingError, match="QKV_PROCESSING_REDUNDANT_OPERATION"):
        apply_output_processing(
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


def test_compare_is_assert_only() -> None:
    with pytest.raises(QKVProcessingError):
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


def test_static_scope_allows_only_prior_derived_variable() -> None:
    """后处理不是变量池查询；不能引用尚未产生的后续派生变量。"""

    with pytest.raises(QKVProcessingError, match="QKV_PROCESSING_UNKNOWN_INPUT"):
        validate_output_processing(
            [
                {
                    "id": "first",
                    "mode": "derive",
                    "input": "{{LATER}}",
                    "operation": "trim",
                    "target_variable": "EARLY",
                },
                {
                    "id": "later",
                    "mode": "derive",
                    "input": "{{DESCRIPTION}}",
                    "operation": "trim",
                    "target_variable": "LATER",
                },
            ],
            available_inputs={"DESCRIPTION"},
        )


def test_multiple_feature_values_use_qfk_aggregation_instead_of_zero_or_more() -> None:
    result = apply_output_processing(
        [{"description": "使用率：80%，峰值：92%，告警值：95%"}],
        [{
            "mode": "assert",
            "input": "{{DESCRIPTION}}",
            "match": {
                "type": "threshold",
                "aggregation": "max",
                "operator": ">",
                "value": 90,
                "expected": True,
            },
        }],
    )
    assert result.matched is True
    assert result.assertions[0].status == "PASS"
