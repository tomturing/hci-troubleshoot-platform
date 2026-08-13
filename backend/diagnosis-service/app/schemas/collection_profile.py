"""采集画像版本契约。"""

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CollectionRequirementLevel(StrEnum):
    """采集项必要性级别。"""

    MANDATORY = "mandatory"
    RECOMMENDED = "recommended"
    CONDITIONAL = "conditional"
    DEEP_DIVE = "deep_dive"


class CollectionCondition(BaseModel):
    """受控条件表达式，禁止执行任意脚本。"""

    model_config = ConfigDict(extra="forbid")

    field: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")
    operator: Literal["eq", "in", "exists"]
    value: Any | None = None

    @model_validator(mode="after")
    def validate_operator_value(self) -> "CollectionCondition":
        """校验条件操作符和值的组合。"""

        if self.operator == "in" and not isinstance(self.value, list):
            raise ValueError("in 条件的 value 必须是数组")
        if self.operator == "exists" and self.value is not None:
            raise ValueError("exists 条件不能提供 value")
        return self


class RelativeTimeWindow(BaseModel):
    """相对故障时刻的采集窗口。"""

    model_config = ConfigDict(extra="forbid")

    before_minutes: int = Field(default=30, ge=0, le=10080)
    after_minutes: int = Field(default=30, ge=0, le=10080)


class CollectionProfileItem(BaseModel):
    """采集画像中的 Collector（采集器）声明。"""

    model_config = ConfigDict(extra="forbid")

    collector_id: str = Field(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_.-]+$")
    display_name: str = Field(min_length=1, max_length=255)
    required_level: CollectionRequirementLevel
    target_scope: Literal["once", "affected_object", "source_node"] = "once"
    condition: CollectionCondition | None = None
    time_window: RelativeTimeWindow = Field(default_factory=RelativeTimeWindow)
    parameters: dict[str, Any] = Field(default_factory=dict)
    reason: str = Field(min_length=1, max_length=2000)
    expected_size_mb: float = Field(default=0, ge=0, le=1048576)
    timeout_seconds: int = Field(default=300, ge=1, le=86400)
    required_permissions: list[str] = Field(default_factory=list, max_length=64)
    sensitive_data_types: list[str] = Field(default_factory=list, max_length=64)

    @model_validator(mode="after")
    def validate_condition(self) -> "CollectionProfileItem":
        """conditional（条件采集）必须带条件，其余级别不得误带条件。"""

        if self.required_level == CollectionRequirementLevel.CONDITIONAL and self.condition is None:
            raise ValueError("conditional 采集项必须配置 condition")
        if self.required_level != CollectionRequirementLevel.CONDITIONAL and self.condition is not None:
            raise ValueError("仅 conditional 采集项可以配置 condition")
        return self


class CollectionProfileDefinition(BaseModel):
    """Collection Profile（采集画像）不可变版本内容。"""

    model_config = ConfigDict(extra="forbid")

    profile_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.\-\u3400-\u9fff]+$")
    display_name: str = Field(min_length=1, max_length=255)
    product_line: Literal["HCI"] = "HCI"
    scenario: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.\-\u3400-\u9fff]+$")
    supported_product_versions: list[str] = Field(min_length=1, max_length=64)
    items: list[CollectionProfileItem] = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def validate_unique_collectors(self) -> "CollectionProfileDefinition":
        """同一画像内不允许重复声明 Collector，且必须有基础必采项。"""

        collector_ids = [item.collector_id for item in self.items]
        if len(collector_ids) != len(set(collector_ids)):
            raise ValueError("同一 Collection Profile 内 collector_id 不能重复")
        if not any(item.required_level == CollectionRequirementLevel.MANDATORY for item in self.items):
            raise ValueError("Collection Profile 至少需要一个 mandatory 采集项")
        if any(not pattern.strip() or len(pattern) > 64 for pattern in self.supported_product_versions):
            raise ValueError("supported_product_versions 每项长度必须为 1-64")
        return self


def profile_requires_affected_object(profile: CollectionProfileDefinition) -> bool:
    """仅当所有初始 mandatory（必需）采集项都依赖业务对象时才全局必填对象标识。"""

    mandatory_items = [
        item for item in profile.items if item.required_level == CollectionRequirementLevel.MANDATORY
    ]
    return bool(mandatory_items) and all(item.target_scope == "affected_object" for item in mandatory_items)


class CollectionProfilePublishRequest(BaseModel):
    """发布采集画像修订版本请求。"""

    model_config = ConfigDict(extra="forbid")

    version: str = Field(min_length=1, max_length=64)
    profile: CollectionProfileDefinition


class CollectionProfileReviewRequest(BaseModel):
    """采集画像审批请求。"""

    model_config = ConfigDict(extra="forbid")

    approved: bool
    reason: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def validate_rejection_reason(self) -> "CollectionProfileReviewRequest":
        """拒绝时必须给出原因。"""

        if not self.approved and not (self.reason or "").strip():
            raise ValueError("拒绝采集画像时必须填写原因")
        return self


class CollectionProfileResponse(BaseModel):
    """采集画像治理状态与当前生效修订响应。"""

    profile: CollectionProfileDefinition
    revision: int | None = None
    version: str
    checksum: str | None = None
    review_status: Literal["draft", "approved", "rejected"] = "approved"
    is_enabled: bool = True
    approved_by: str | None = None
    approved_at: str | None = None
    rejection_reason: str | None = None
    lock_version: int = 1
    managed_by: Literal["manual", "kbd_sync"] = "manual"
    generation_metadata: dict[str, Any] = Field(default_factory=dict)
    trace_id: str | None = None
    published_at: str | None = None


class OfflineScenarioOptionResponse(BaseModel):
    """面向客户的可用离线诊断场景，不暴露采集命令等治理细节。"""

    scenario: str
    display_name: str
    profile_revision: int
    profile_version: str
    supported_product_versions: list[str]
    requires_affected_object: bool
