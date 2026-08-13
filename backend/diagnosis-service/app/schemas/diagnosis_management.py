"""离线诊断管理工作台 API 契约。"""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class DiagnosisSessionManagementItem(BaseModel):
    """管理端诊断会话列表项。"""

    session_id: UUID
    case_id: str
    customer_id: str | None
    selected_scenario: str
    status: str
    assigned_to: str | None
    supplement_count: int
    latest_report_status: str | None
    latest_report_sequence: int | None
    bundle_count: int
    failed_task_count: int
    trace_id: str
    created_at: datetime
    updated_at: datetime


class DiagnosisSessionManagementList(BaseModel):
    """管理端诊断会话分页列表。"""

    items: list[DiagnosisSessionManagementItem]
    total: int
    offset: int
    limit: int


class AssignDiagnosisSessionRequest(BaseModel):
    """转派诊断会话请求。"""

    model_config = ConfigDict(extra="forbid")
    assigned_to: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:@-]+$")


class TerminateDiagnosisSessionRequest(BaseModel):
    """终止诊断会话请求。"""

    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=2, max_length=2000)


class SecurityEventReviewRequest(BaseModel):
    """隔离区安全事件处置请求。"""

    model_config = ConfigDict(extra="forbid")
    action: Literal["acknowledge", "clear"]
    note: str = Field(min_length=2, max_length=2000)


class DiagnosisManagementActionResponse(BaseModel):
    """管理动作统一响应。"""

    resource_id: str
    status: str
    trace_id: str
    details: dict[str, Any] = Field(default_factory=dict)


class DiagnosisManagementRecord(BaseModel):
    """管理端聚合记录。"""

    record_type: str
    resource_id: str
    session_id: UUID | None
    status: str | None
    occurred_at: datetime
    trace_id: str
    details: dict[str, Any] = Field(default_factory=dict)
