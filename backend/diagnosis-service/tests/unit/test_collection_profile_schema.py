"""采集画像契约测试。"""

import pytest
from app.schemas.collection_profile import CollectionProfileDefinition, profile_requires_affected_object
from pydantic import ValidationError


def make_profile() -> dict:
    """构造合法采集画像。"""

    return {
        "profile_id": "vm_backup_failed",
        "display_name": "虚拟机备份失败采集画像",
        "scenario": "vm_backup_failed",
        "supported_product_versions": ["6.*", "7.0.0"],
        "items": [
            {
                "collector_id": "hci.task.failed",
                "display_name": "失败任务",
                "required_level": "mandatory",
                "target_scope": "affected_object",
                "reason": "获取故障窗口内失败任务",
            },
            {
                "collector_id": "hci.alert.active",
                "display_name": "活动告警",
                "required_level": "conditional",
                "condition": {"field": "alert.enabled", "operator": "eq", "value": True},
                "reason": "告警开启时采集",
            },
        ],
    }


def test_profile_accepts_controlled_conditional_item():
    """合法条件采集项通过校验。"""

    profile = CollectionProfileDefinition.model_validate(make_profile())

    assert profile.items[1].condition.field == "alert.enabled"
    assert profile.supported_product_versions == ["6.*", "7.0.0"]
    assert profile_requires_affected_object(profile) is True


def test_mixed_mandatory_profile_does_not_globally_require_object_id():
    payload = make_profile()
    payload["items"].append(
        {
            "collector_id": "hci.task.cluster",
            "display_name": "集群失败任务",
            "required_level": "mandatory",
            "target_scope": "source_node",
            "reason": "对象创建失败时从任务上下文定位",
        }
    )

    profile = CollectionProfileDefinition.model_validate(payload)
    assert profile_requires_affected_object(profile) is False


def test_profile_accepts_shared_chinese_category_code_as_identity():
    """KBD 最终分类编码可直接作为在线、离线共用场景和画像标识。"""

    payload = make_profile()
    payload["profile_id"] = "虚拟机-027"
    payload["scenario"] = "虚拟机-027"

    profile = CollectionProfileDefinition.model_validate(payload)

    assert profile.profile_id == "虚拟机-027"


def test_profile_rejects_path_separator_in_identity():
    """画像标识会进入 API 路径，不能接受路径分隔符。"""

    payload = make_profile()
    payload["profile_id"] = "虚拟机/027"
    payload["scenario"] = "虚拟机/027"

    with pytest.raises(ValidationError):
        CollectionProfileDefinition.model_validate(payload)


def test_conditional_item_requires_condition():
    """conditional 采集项缺少条件时拒绝。"""

    payload = make_profile()
    del payload["items"][1]["condition"]

    with pytest.raises(ValidationError):
        CollectionProfileDefinition.model_validate(payload)


def test_profile_requires_mandatory_item():
    """画像至少包含一个 mandatory 必采项。"""

    payload = make_profile()
    payload["items"][0]["required_level"] = "recommended"

    with pytest.raises(ValidationError):
        CollectionProfileDefinition.model_validate(payload)


def test_profile_rejects_duplicate_collector_id():
    """画像拒绝重复 Collector 标识。"""

    payload = make_profile()
    payload["items"][1]["collector_id"] = payload["items"][0]["collector_id"]

    with pytest.raises(ValidationError):
        CollectionProfileDefinition.model_validate(payload)
