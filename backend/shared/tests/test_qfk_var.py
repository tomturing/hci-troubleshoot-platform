"""qfk_var 四层处理器的确定性契约测试。"""

from shared.signals.variable_processor import (
    execute_operation,
    extract_deterministic_candidates,
    feature_extract,
    get_path_value,
)

MEMORY_DESCRIPTION = "主机（SVR_aCloud_670）的计算内存使用量（92.35 GB）超过阈值（92.34 GB），剩余：7.99 GB，使用率：92%"


def test_real_hci_description_extracts_labeled_variables() -> None:
    assert feature_extract(MEMORY_DESCRIPTION, "host").value == "SVR_aCloud_670"
    assert feature_extract(MEMORY_DESCRIPTION, "memory.used").value["value"] == 92.35
    assert feature_extract(MEMORY_DESCRIPTION, "memory.threshold").value["value"] == 92.34
    assert feature_extract(MEMORY_DESCRIPTION, "memory.remaining").value["value"] == 7.99
    assert feature_extract(MEMORY_DESCRIPTION, "percent.current").value == 92.0


def test_common_hci_feature_shapes() -> None:
    assert feature_extract("虚拟机（Ubuntu-26.04）软重启", "vm_name").value == "Ubuntu-26.04"
    assert feature_extract("源主机：A，目的主机：B", "source_host").value == "A"
    assert feature_extract("源主机：A，目的主机：B", "destination_host").value == "B"
    assert feature_extract("从（100）变更为（200）", "change_pair").value == {"from": "100", "to": "200"}
    assert feature_extract("错误码：E-1234", "error_code").value == "E-1234"


def test_first_layer_keeps_ip_and_version_as_single_or_ignored_candidates() -> None:
    candidates = extract_deterministic_candidates("IP：172.28.24.1，版本：1.2.3，名称：vm-001")
    values = {item.raw_value for item in candidates}
    assert "172.28.24.1" in values
    assert "1.2.3" in values
    assert "172.28" not in values
    assert "1.2" not in values
    assert "vm-001" in values


def test_stable_multiple_candidates_are_ambiguous_and_never_first_match() -> None:
    result = feature_extract("主机（node-a）和主机（node-b）均异常", "host")
    assert result.status == "ambiguous"
    assert result.error_code == "QFK_VAR_CARDINALITY_MISMATCH"
    assert result.raw_values == ["node-a", "node-b"]


def test_compare_percentage_quantity_and_unit_mismatch() -> None:
    assert execute_operation({"operation": "compare", "left": "91%", "right": "90%", "operator": ">", "value_type": "percentage"}).matched is True
    assert execute_operation({"operation": "compare", "left": "1 GB", "right": "2 GB", "operator": "<", "value_type": "quantity"}).matched is True
    assert execute_operation({"operation": "compare", "left": "1 GB", "right": "2 GiB", "operator": "<", "value_type": "quantity"}).error_code == "QFK_VAR_UNIT_MISMATCH"


def test_json_path_and_split_are_bounded_operations() -> None:
    assert get_path_value({"data": [{"vm": "vm-001"}]}, "$.data[0].vm") == "vm-001"
    result = execute_operation({"operation": "string", "input": " a, b ", "function": "split", "separator": ","})
    assert result.value == ["a", "b"]
