"""诊断会话请求和响应结构。"""

from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.session_state import DiagnosisSessionStatus


class AffectedObject(BaseModel):
    """受影响对象。"""

    type: str = Field(min_length=1, max_length=64)
    id: str | None = Field(default=None, min_length=1, max_length=255)
    name: str | None = Field(default=None, max_length=255)
    source_node: str | None = Field(default=None, max_length=255)


class IncidentContext(BaseModel):
    """故障时间上下文。"""

    start_time: datetime
    end_time: datetime
    timezone: str = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def validate_incident_window(self) -> "IncidentContext":
        """校验时区和故障时间窗口。"""

        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("timezone 必须是有效的 IANA 时区") from exc

        if self.start_time.tzinfo is None or self.end_time.tzinfo is None:
            raise ValueError("故障开始和结束时间必须包含时区")
        if self.end_time < self.start_time:
            raise ValueError("故障结束时间不能早于开始时间")
        return self


class DiagnosisSessionCreate(BaseModel):
    """创建诊断会话请求。"""

    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(pattern=r"^Q\d{12,13}$")
    product_line: Literal["HCI"] = "HCI"
    selected_scenario: str = Field(min_length=1, max_length=100)
    selected_category: str | None = Field(default=None, max_length=100)
    incident: IncidentContext
    affected_objects: list[AffectedObject] = Field(default_factory=list, max_length=64)
    impact_scope: str = Field(min_length=1, max_length=64)
    current_status: Literal["ongoing", "recovered", "intermittent"]
    recent_change_description: str | None = Field(default=None, max_length=4000)
    experimental: bool = False

class DiagnosisSessionResponse(BaseModel):
    """诊断会话响应。"""

    session_id: str
    case_id: str
    tenant_id: str
    assigned_to: str | None
    product_line: str
    selected_scenario: str
    selected_category: str | None
    resolved_category: str | None
    incident: IncidentContext
    affected_objects: list[AffectedObject]
    impact_scope: str
    current_status: str
    experimental: bool
    status: DiagnosisSessionStatus
    supplement_count: int
    version: int
    trace_id: str
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_entity(cls, entity) -> "DiagnosisSessionResponse":
        """将 ORM 实体转换为稳定的 API 响应。"""

        return cls(
            session_id=str(entity.session_id),
            case_id=entity.case_id,
            tenant_id=entity.tenant_id,
            assigned_to=getattr(entity, "assigned_to", None),
            product_line=entity.product_line,
            selected_scenario=entity.selected_scenario,
            selected_category=entity.selected_category,
            resolved_category=entity.resolved_category,
            incident=IncidentContext(
                start_time=entity.incident_start_time,
                end_time=entity.incident_end_time,
                timezone=entity.incident_timezone,
            ),
            affected_objects=[AffectedObject.model_validate(item) for item in entity.affected_objects],
            impact_scope=entity.impact_scope,
            current_status=entity.incident_status,
            experimental=bool(entity.experimental),
            status=DiagnosisSessionStatus(entity.status),
            supplement_count=entity.supplement_count,
            version=entity.version,
            trace_id=entity.trace_id,
            created_at=entity.created_at,
            updated_at=entity.updated_at,
        )
