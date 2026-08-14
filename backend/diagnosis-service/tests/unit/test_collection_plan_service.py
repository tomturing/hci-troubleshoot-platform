"""采集计划展开测试。"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from app.errors import DiagnosisError
from app.schemas.collection_profile import CollectionProfileDefinition
from app.services.collection_plan_service import CollectionPlanService


def make_profile() -> CollectionProfileDefinition:
    """构造覆盖四级采集策略的画像。"""

    return CollectionProfileDefinition.model_validate(
        {
            "profile_id": "vm_backup_failed",
            "display_name": "虚拟机备份失败",
            "scenario": "vm_backup_failed",
            "supported_product_versions": ["6.*"],
            "items": [
                {
                    "collector_id": "hci.task.failed",
                    "display_name": "失败任务",
                    "required_level": "mandatory",
                    "target_scope": "affected_object",
                    "time_window": {"before_minutes": 10, "after_minutes": 20},
                    "reason": "定位失败任务",
                    "expected_size_mb": 2.5,
                    "timeout_seconds": 60,
                    "required_permissions": ["task.read"],
                },
                {
                    "collector_id": "hci.node.log",
                    "display_name": "节点日志",
                    "required_level": "recommended",
                    "target_scope": "source_node",
                    "parameters": {"keyword": "backup", "file": "vtpdaemon.log"},
                    "reason": "关联节点日志",
                    "expected_size_mb": 10,
                    "timeout_seconds": 120,
                    "sensitive_data_types": ["ip_address"],
                },
                {
                    "collector_id": "hci.alert.active",
                    "display_name": "活动告警",
                    "required_level": "conditional",
                    "condition": {"field": "alert.enabled", "operator": "eq", "value": True},
                    "reason": "仅在告警开启时采集",
                },
                {
                    "collector_id": "hci.deep.full_log",
                    "display_name": "全量日志",
                    "required_level": "deep_dive",
                    "reason": "补充诊断时才采集",
                },
            ],
        }
    )


def test_product_version_validation_accepts_comparison_constraint():
    profile = make_profile().model_copy(update={"supported_product_versions": [">=6.12.0,<7.0.0"]})

    CollectionPlanService._validate_product_version(profile, "6.12.0")
    with pytest.raises(DiagnosisError, match="不支持该产品版本"):
        CollectionPlanService._validate_product_version(profile, "6.11.9")


def make_session(*, source_node: str | None = "node-1"):
    """构造诊断会话。"""

    start = datetime(2026, 7, 29, 9, 0, tzinfo=UTC)
    return SimpleNamespace(
        session_id=uuid4(),
        incident_start_time=start,
        incident_end_time=start + timedelta(minutes=30),
        incident_timezone="Asia/Shanghai",
        affected_objects=[
            {"type": "vm", "id": "vm-1", "source_node": source_node},
            {"type": "vm", "id": "vm-2", "source_node": source_node},
        ],
    )


def test_expand_profile_expands_targets_conditions_and_summary():
    """画像按对象/节点展开，并汇总权限、容量和敏感数据。"""

    items, summary = CollectionPlanService.expand_profile(
        profile=make_profile(),
        diagnosis_session=make_session(),
        context={"alert": {"enabled": True}},
        trace_id="a" * 32,
    )

    assert [item["collector_id"] for item in items] == [
        "hci.task.failed",
        "hci.task.failed",
        "hci.node.log",
        "hci.alert.active",
        "hci.deep.full_log",
    ]
    assert items[0]["time_window"]["start_time"] == "2026-07-29T08:50:00+00:00"
    assert items[0]["time_window"]["end_time"] == "2026-07-29T09:50:00+00:00"
    node_item = next(item for item in items if item["collector_id"] == "hci.node.log")
    assert node_item["collector_parameters"] == {"keyword": "backup", "file": "vtpdaemon.log"}
    assert summary["required_permissions"] == ["task.read"]
    assert summary["sensitive_data_types"] == ["ip_address"]
    assert summary["estimated_size_mb"] == 15.0
    assert summary["estimated_duration_seconds"] == 540
    assert summary["unresolved_variables"] == []
    assert items[-1]["activation_state"] == "deferred"


def test_expand_profile_skips_false_condition_and_defers_deep_dive():
    """初始计划排除条件不满足项，并保留 deferred 深度采集候选。"""

    items, _summary = CollectionPlanService.expand_profile(
        profile=make_profile(),
        diagnosis_session=make_session(),
        context={"alert": {"enabled": False}},
        trace_id="a" * 32,
    )

    assert "hci.alert.active" not in {item["collector_id"] for item in items}
    deep_item = next(item for item in items if item["collector_id"] == "hci.deep.full_log")
    assert deep_item["activation_state"] == "deferred"


def test_expand_profile_marks_missing_source_node():
    """缺少源节点时保留变量占位并声明未解析变量。"""

    items, summary = CollectionPlanService.expand_profile(
        profile=make_profile(),
        diagnosis_session=make_session(source_node=None),
        context={},
        trace_id="a" * 32,
    )

    node_item = next(item for item in items if item["collector_id"] == "hci.node.log")
    assert node_item["target"] == {"type": "variable", "id": "source_node"}
    assert summary["unresolved_variables"] == ["source_node"]


def test_expand_profile_omits_object_only_item_when_object_does_not_exist():
    """混合画像没有对象 ID 时跳过对象级采集项，不生成空 target_id 命令。"""

    diagnosis_session = make_session()
    diagnosis_session.affected_objects = [{"type": "execution_node", "source_node": "node-1"}]
    items, summary = CollectionPlanService.expand_profile(
        profile=make_profile(),
        diagnosis_session=diagnosis_session,
        context={},
        trace_id="a" * 32,
    )

    assert "hci.task.failed" not in {item["collector_id"] for item in items}
    assert "hci.node.log" in {item["collector_id"] for item in items}
    assert summary["unresolved_variables"] == []
