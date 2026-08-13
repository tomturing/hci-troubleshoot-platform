"""KBD 驱动的离线采集资源同步契约。"""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class OfflineResourceSyncPreviewRequest(BaseModel):
    """创建同步候选批次。"""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["incremental", "full"] = "incremental"


class OfflineResourceSyncDecisionRequest(BaseModel):
    """发布、拒绝或回滚同步批次。"""

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=2, max_length=2000)


class OfflineResourceSyncChangeResponse(BaseModel):
    """单项 Collector/Profile/Mapping 差异。"""

    change_id: UUID
    batch_id: UUID
    resource_type: str
    resource_name: str
    change_type: str
    status: str
    source_kbd_ids: list[int]
    source_kbd_revisions: list[dict[str, Any]] = Field(default_factory=list)
    source_tool_revisions: list[dict[str, Any]] = Field(default_factory=list)
    before_revision: int | None
    after_revision: int | None
    before_governance_json: dict[str, Any] = Field(default_factory=dict)
    candidate_governance_json: dict[str, Any] = Field(default_factory=dict)
    before_json: dict[str, Any] | None
    candidate_json: dict[str, Any] | None
    after_json: dict[str, Any] | None
    validation_json: list[dict[str, Any]]
    trace_id: str
    created_at: datetime
    updated_at: datetime


class OfflineResourceSyncEventResponse(BaseModel):
    """追加式同步动作审计事件。"""

    event_id: UUID
    batch_id: UUID
    event_sequence: int
    action: str
    result: str
    actor_id: str
    details_json: dict[str, Any]
    trace_id: str
    created_at: datetime


class OfflineResourceSyncBatchResponse(BaseModel):
    """同步批次及可选的差异、动作历史。"""

    batch_id: UUID
    base_cursor: int
    target_cursor: int
    base_tool_cursor: int = 0
    target_tool_cursor: int = 0
    sync_mode: str
    status: str
    requested_by: str
    approved_by: str | None
    rollback_by: str | None
    approval_reason: str | None
    rollback_reason: str | None
    kbd_change_count: int
    tool_change_count: int = 0
    collector_change_count: int
    profile_change_count: int
    mapping_change_count: int
    summary_json: dict[str, Any]
    validation_json: list[dict[str, Any]]
    error_json: dict[str, Any]
    trace_id: str
    created_at: datetime
    published_at: datetime | None
    rolled_back_at: datetime | None
    updated_at: datetime
    changes: list[OfflineResourceSyncChangeResponse] = Field(default_factory=list)
    events: list[OfflineResourceSyncEventResponse] = Field(default_factory=list)


class OfflineResourceSyncHistoryResponse(BaseModel):
    """同步历史分页结果。"""

    items: list[OfflineResourceSyncBatchResponse]
    total: int
    offset: int
    limit: int
