"""诊断会话 Schema 测试。"""

from datetime import UTC, datetime, timedelta

import pytest
from app.schemas.diagnosis_session import DiagnosisSessionCreate
from pydantic import ValidationError


def make_payload() -> dict:
    """构造合法的创建请求。"""

    start = datetime(2026, 7, 29, 9, 0, tzinfo=UTC)
    return {
        "case_id": "Q2026072900001",
        "product_line": "HCI",
        "selected_scenario": "vm_backup_failed",
        "selected_category": "虚拟机备份与CDP",
        "incident": {
            "start_time": start,
            "end_time": start + timedelta(minutes=30),
            "timezone": "Asia/Shanghai",
        },
        "affected_objects": [{"type": "vm", "id": "vm-027"}],
        "impact_scope": "single_vm",
        "current_status": "ongoing",
    }


def test_supported_scenario_is_accepted():
    """正式 P0 场景可以创建。"""

    command = DiagnosisSessionCreate.model_validate(make_payload())
    assert command.selected_scenario == "vm_backup_failed"


def test_historical_four_digit_case_sequence_is_accepted():
    """兼容历史四位日序号工单。"""

    payload = make_payload()
    payload["case_id"] = "Q202602220001"
    command = DiagnosisSessionCreate.model_validate(payload)
    assert command.case_id == "Q202602220001"


def test_scenario_identifier_is_not_limited_by_static_whitelist():
    """Schema 接受动态场景标识，业务服务再校验对应画像是否可用。"""

    payload = make_payload()
    payload["selected_scenario"] = "unknown_scene"

    command = DiagnosisSessionCreate.model_validate(payload)
    assert command.selected_scenario == "unknown_scene"


def test_session_allows_execution_node_without_created_business_object():
    """创建失败场景允许只提供采集执行节点，不伪造尚未产生的对象 ID。"""

    payload = make_payload()
    payload["affected_objects"] = [{"type": "execution_node", "source_node": "10.97.128.10"}]

    command = DiagnosisSessionCreate.model_validate(payload)
    assert command.affected_objects[0].id is None


def test_session_allows_empty_affected_objects_for_non_node_collectors():
    payload = make_payload()
    payload["affected_objects"] = []

    command = DiagnosisSessionCreate.model_validate(payload)
    assert command.affected_objects == []


def test_incident_window_requires_timezone_aware_values():
    """故障时间必须携带时区。"""

    payload = make_payload()
    payload["incident"]["start_time"] = datetime(2026, 7, 29, 9, 0)

    with pytest.raises(ValidationError, match="必须包含时区"):
        DiagnosisSessionCreate.model_validate(payload)


def test_incident_end_cannot_precede_start():
    """结束时间不能早于开始时间。"""

    payload = make_payload()
    payload["incident"]["end_time"] = payload["incident"]["start_time"] - timedelta(seconds=1)

    with pytest.raises(ValidationError, match="不能早于开始时间"):
        DiagnosisSessionCreate.model_validate(payload)


def test_invalid_iana_timezone_is_rejected():
    """拒绝无效 IANA 时区。"""

    payload = make_payload()
    payload["incident"]["timezone"] = "Mars/Olympus"

    with pytest.raises(ValidationError, match="IANA 时区"):
        DiagnosisSessionCreate.model_validate(payload)
