"""离线证据上传、分析、报告和删除契约。"""

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class BundleType(StrEnum):
    """Diagnostic Evidence Bundle（诊断证据包）类型。"""

    INITIAL = "initial"
    SUPPLEMENT = "supplement"
    VERIFICATION = "verification"


class UploadSessionCreate(BaseModel):
    """创建分片直传会话。"""

    model_config = ConfigDict(extra="forbid")

    bundle_type: BundleType
    parent_bundle_id: UUID | None = None
    collection_plan_id: UUID
    collector_artifact_id: UUID
    file_name: str = Field(min_length=1, max_length=255, pattern=r"^[^/\\\x00]+$")
    media_type: Literal["application/gzip", "application/x-gzip", "application/vnd.hci.evidence"]
    total_size_bytes: int = Field(gt=0, le=512 * 1024 * 1024)
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    chunk_size_bytes: int = Field(default=16 * 1024 * 1024, ge=1024 * 1024, le=64 * 1024 * 1024)

    @model_validator(mode="after")
    def validate_parent(self) -> "UploadSessionCreate":
        """补采包必须引用父包，初始包不得引用。"""

        if self.bundle_type == BundleType.SUPPLEMENT and self.parent_bundle_id is None:
            raise ValueError("supplement 包必须提供 parent_bundle_id")
        if self.bundle_type == BundleType.INITIAL and self.parent_bundle_id is not None:
            raise ValueError("initial 包不能提供 parent_bundle_id")
        return self


class UploadPartTarget(BaseModel):
    """对象存储分片直传目标。"""

    part_number: int
    upload_url: str
    expires_at: datetime


class UploadSessionResponse(BaseModel):
    """上传会话响应。"""

    upload_id: UUID
    session_id: UUID
    status: str
    bundle_type: BundleType
    total_size_bytes: int
    chunk_size_bytes: int
    part_count: int
    uploaded_parts: dict[str, Any]
    upload_token: str | None = None
    upload_targets: list[UploadPartTarget] = Field(default_factory=list)
    expires_at: datetime
    trace_id: str


class UploadPartResponse(BaseModel):
    """分片写入结果。"""

    upload_id: UUID
    part_number: int
    size_bytes: int
    sha256: str
    status: str


class UploadCompleteRequest(BaseModel):
    """确认上传完成。"""

    model_config = ConfigDict(extra="forbid")

    parts: list[int] = Field(min_length=1, max_length=10000)

    @field_validator("parts")
    @classmethod
    def validate_parts(cls, value: list[int]) -> list[int]:
        """分片编号必须从 1 开始且不重复。"""

        if any(item < 1 for item in value) or len(value) != len(set(value)):
            raise ValueError("parts 必须是互不重复的正整数")
        return value


class EvidenceBundleResponse(BaseModel):
    """诊断证据包响应。"""

    bundle_id: UUID
    session_id: UUID
    bundle_type: BundleType
    parent_bundle_id: UUID | None
    collection_plan_id: UUID
    collector_artifact_id: UUID
    size_bytes: int
    sha256: str
    schema_version: str | None
    processing_status: str
    security_results: dict[str, Any]
    failure_code: str | None
    failure_message: str | None
    retention_until: datetime
    legal_hold: bool
    version: int
    trace_id: str
    created_at: datetime
    updated_at: datetime


class EvidenceQueryRequest(BaseModel):
    """Offline Evidence Provider（离线证据查询器）请求。"""

    model_config = ConfigDict(extra="forbid")

    query_type: Literal["log", "json", "command_output", "metric", "evidence_status"]
    collector_id: str | None = Field(default=None, max_length=128)
    source_path: str | None = Field(default=None, max_length=2048)
    keyword: str | None = Field(default=None, max_length=512)
    json_path: str | None = Field(default=None, max_length=512)
    field: str | None = Field(default=None, max_length=512)
    start_time: datetime | None = None
    end_time: datetime | None = None
    limit: int = Field(default=50, ge=1, le=200)

    @model_validator(mode="after")
    def validate_time_window(self) -> "EvidenceQueryRequest":
        """查询时间窗必须有序。"""

        if self.start_time and self.end_time and self.end_time < self.start_time:
            raise ValueError("end_time 不能早于 start_time")
        return self


class EvidenceQueryResult(BaseModel):
    """离线证据查询结果。"""

    query_id: UUID
    status: str
    evidence: list[dict[str, Any]]
    trace_id: str


class EvidenceAssessmentResponse(BaseModel):
    """Evidence Assessment（证据评估）响应。"""

    assessment_id: UUID
    session_id: UUID
    bundle_ids: list[UUID]
    algorithm_version: str
    completeness_score: int
    mandatory: dict[str, int]
    missing_evidence: list[dict[str, Any]]
    diagnosable_scope: list[str]
    non_diagnosable_scope: list[str]
    ready_for_diagnosis: bool
    calculation_details: dict[str, Any]
    trace_id: str
    created_at: datetime


class SupplementPlanResponse(BaseModel):
    """Supplement Plan（补充采集计划）响应。"""

    supplement_plan_id: UUID
    collection_plan_id: UUID
    parent_bundle_id: UUID
    session_id: UUID
    run_id: UUID
    reason: str
    confirmed_findings: list[str]
    unconfirmed_findings: list[str]
    collection_items: list[dict[str, Any]]
    expected_size_mb: float
    expected_duration_minutes: int
    status: str
    trace_id: str
    created_at: datetime
    updated_at: datetime


class DiagnosisRunResponse(BaseModel):
    """Diagnosis Run（诊断运行）响应。"""

    run_id: UUID
    session_id: UUID
    assessment_id: UUID
    run_sequence: int
    status: str
    selected_category: str | None
    resolved_category: str | None
    run_manifest: dict[str, Any]
    conclusion_policy_version: str
    matcher_version: str
    agent_version: str
    trace_id: str
    created_at: datetime
    completed_at: datetime | None


class SignalEvaluationResponse(BaseModel):
    """Signal Evaluation（三态信号评估）响应。"""

    evaluation_id: UUID
    run_id: UUID
    signal_id: str
    state: Literal["MATCHED", "NOT_MATCHED", "UNKNOWN"]
    reason: str
    required_for_conclusion: bool
    evidence_status: str
    evidence_refs: list[Any]
    matcher_snapshot: dict[str, Any] | None
    trace_id: str
    created_at: datetime


class DiagnosisCandidateResponse(BaseModel):
    """Diagnosis Candidate（诊断候选）响应。"""

    candidate_id: UUID
    run_id: UUID
    kbd_id: int | None
    support_id: str | None
    title: str
    category_id: str | None
    score: float
    matched_count: int
    not_matched_count: int
    unknown_count: int
    signal_coverage: float
    kbd_snapshot: dict[str, Any]
    trace_id: str
    created_at: datetime


class DiagnosisReportResponse(BaseModel):
    """Diagnosis Report（诊断报告）响应。"""

    report_id: UUID
    session_id: UUID
    run_id: UUID
    report_sequence: int
    diagnosis_level: Literal["Confirmed", "Probable", "Suspected", "Insufficient", "Conflicted"]
    summary: str
    resolved_domain: str | None
    primary_hypothesis: str | None
    confidence: float
    supporting_evidence: list[dict[str, Any]]
    counter_evidence: list[dict[str, Any]]
    excluded_causes: list[dict[str, Any]]
    missing_evidence: list[dict[str, Any]]
    recommended_recovery: list[dict[str, Any]]
    risk_and_rollback: list[dict[str, Any]]
    root_cause_validation: list[dict[str, Any]]
    supplement_plan_id: UUID | None
    matched_kbds: list[dict[str, Any]]
    publish_status: str
    conclusion_policy_version: str
    report_schema_version: str
    version: int
    trace_id: str
    created_at: datetime
    updated_at: datetime


class ReportReviewRequest(BaseModel):
    """报告审核、编辑和发布请求。"""

    model_config = ConfigDict(extra="forbid")

    action: Literal["submit_review", "confirm", "publish", "reject", "return_to_draft"]
    reason: str = Field(min_length=2, max_length=2000)
    summary: str | None = Field(default=None, min_length=1, max_length=10000)
    recommended_recovery: list[dict[str, Any]] | None = Field(default=None, max_length=100)


class DiagnosisStartRequest(BaseModel):
    """显式触发或重放诊断。"""

    model_config = ConfigDict(extra="forbid")

    use_latest_rules: bool = False


class DeletionRequest(BaseModel):
    """发起诊断数据删除。"""

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=2, max_length=2000)


class LegalHoldRequest(BaseModel):
    """设置或解除 Legal Hold（法务保全）。"""

    model_config = ConfigDict(extra="forbid")

    action: Literal["apply", "release"]
    reason: str = Field(min_length=2, max_length=2000)


class LegalHoldResponse(BaseModel):
    """法务保全状态及最新审计响应。"""

    session_id: UUID
    legal_hold: bool
    affected_bundle_ids: list[UUID]
    latest_action: str | None
    latest_actor_id: str | None
    latest_reason: str | None
    updated_at: datetime | None


class OfflineSignalMappingWrite(BaseModel):
    """Offline Signal Mapping（离线信号映射）写入契约。"""

    model_config = ConfigDict(extra="forbid")

    source_kbd_id: int = Field(gt=0)
    source_kbd_revision: int = Field(gt=0)
    source_signal_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    execution_contract_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    acquire_tool: str = Field(min_length=2, max_length=64, pattern=r"^[a-z][a-z0-9_]+$")
    category_scope: str = Field(default="*", min_length=1, max_length=100)
    command_scope: str = Field(default="*", min_length=1, max_length=128)
    collector_id: str = Field(min_length=2, max_length=128, pattern=r"^[a-z][a-z0-9_.-]+$")
    query_type: Literal["log", "json", "command_output", "metric", "evidence_status"] = "command_output"
    field_mapping: dict[str, str] = Field(default_factory=dict)
    priority: int = Field(default=100, ge=0, le=10000)
    is_enabled: bool = True

    @field_validator("category_scope", "command_scope")
    @classmethod
    def validate_scope(cls, value: str) -> str:
        """通配范围只允许单个 *，其余值不得含通配表达式。"""

        normalized = value.strip()
        if "*" in normalized and normalized != "*":
            raise ValueError("scope 只能是精确值或单个 *")
        return normalized


class OfflineSignalMappingResponse(BaseModel):
    """离线信号映射响应。"""

    mapping_id: UUID
    source_kbd_id: int | None
    source_kbd_revision: int | None
    source_signal_id: str | None
    execution_contract_checksum: str | None
    acquire_tool: str
    category_scope: str
    command_scope: str
    collector_id: str
    query_type: str
    field_mapping: dict[str, str]
    priority: int
    is_enabled: bool
    lock_version: int
    trace_id: str
    created_at: datetime
    updated_at: datetime


class DiagnosisTimelineEvent(BaseModel):
    """Diagnosis Timeline（诊断时间线）事件。"""

    event_type: str
    event_id: str
    status: str | None
    occurred_at: datetime
    trace_id: str | None
    details: dict[str, Any] = Field(default_factory=dict)


class DeletionJobResponse(BaseModel):
    """异步删除任务响应。"""

    deletion_id: UUID
    session_id: UUID
    status: str
    deletion_results: dict[str, Any]
    failure_message: str | None
    trace_id: str
    created_at: datetime
    updated_at: datetime
