"""采集计划请求和响应契约。"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class CollectionPlanGenerateRequest(BaseModel):
    """生成初始 Collection Plan（采集计划）的请求。"""

    model_config = ConfigDict(extra="forbid")

    product_version: str = Field(min_length=1, max_length=64)
    context: dict[str, Any] = Field(default_factory=dict)


class CollectionPlanRegenerateRequest(BaseModel):
    """基于最新画像和 KBD 规则集重生成采集计划。"""

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=2, max_length=2000)


class CollectionPlanItemResponse(BaseModel):
    """采集计划执行项响应。"""

    item_id: str
    sequence: int
    collector_id: str
    collector_revision: int | None
    collector_version: str | None
    collector_checksum: str | None
    display_name: str
    required_level: str
    activation_state: str
    target: dict[str, Any]
    time_window: dict[str, Any]
    collector_parameters: dict[str, Any]
    condition_snapshot: dict[str, Any] | None
    reason: str
    expected_size_mb: float
    timeout_seconds: int
    required_permissions: list[str]
    sensitive_data_types: list[str]

    @classmethod
    def from_entity(cls, entity) -> "CollectionPlanItemResponse":
        """将采集计划项实体转换为响应。"""

        return cls(
            item_id=str(entity.item_id),
            sequence=entity.sequence,
            collector_id=entity.collector_id,
            collector_revision=getattr(entity, "collector_revision", None),
            collector_version=getattr(entity, "collector_version", None),
            collector_checksum=getattr(entity, "collector_checksum", None),
            display_name=entity.display_name,
            required_level=entity.required_level,
            activation_state=entity.activation_state,
            target=dict(entity.target or {}),
            time_window=dict(entity.time_window or {}),
            collector_parameters=dict(getattr(entity, "collector_parameters", None) or {}),
            condition_snapshot=dict(entity.condition_snapshot) if entity.condition_snapshot else None,
            reason=entity.reason,
            expected_size_mb=float(entity.expected_size_mb),
            timeout_seconds=entity.timeout_seconds,
            required_permissions=list(entity.required_permissions or []),
            sensitive_data_types=list(entity.sensitive_data_types or []),
        )


class CollectionPlanResponse(BaseModel):
    """采集计划详情响应。"""

    collection_plan_id: str
    session_id: str
    plan_sequence: int
    plan_revision: int
    profile_name: str
    profile_revision: int
    profile_version: str
    profile_checksum: str
    product_version: str
    kbd_ruleset_snapshot: list[dict[str, Any]]
    kbd_ruleset_checksum: str
    required_permissions: list[str]
    sensitive_data_types: list[str]
    unresolved_variables: list[str]
    estimated_size_mb: float
    estimated_duration_seconds: int
    status: str
    trace_id: str
    created_at: datetime
    updated_at: datetime
    items: list[CollectionPlanItemResponse]

    @classmethod
    def from_entities(cls, plan, items) -> "CollectionPlanResponse":
        """将计划和执行项转换为稳定响应。"""

        return cls(
            collection_plan_id=str(plan.plan_id),
            session_id=str(plan.session_id),
            plan_sequence=plan.plan_sequence,
            plan_revision=getattr(plan, "plan_revision", 1),
            profile_name=plan.profile_name,
            profile_revision=plan.profile_revision,
            profile_version=plan.profile_version,
            profile_checksum=plan.profile_checksum,
            product_version=plan.product_version,
            kbd_ruleset_snapshot=list(getattr(plan, "kbd_ruleset_snapshot", []) or []),
            kbd_ruleset_checksum=getattr(plan, "kbd_ruleset_checksum", ""),
            required_permissions=list(plan.required_permissions or []),
            sensitive_data_types=list(plan.sensitive_data_types or []),
            unresolved_variables=list(plan.unresolved_variables or []),
            estimated_size_mb=float(plan.estimated_size_mb),
            estimated_duration_seconds=plan.estimated_duration_seconds,
            status=plan.status,
            trace_id=plan.trace_id,
            created_at=plan.created_at,
            updated_at=plan.updated_at,
            items=[CollectionPlanItemResponse.from_entity(item) for item in items],
        )
