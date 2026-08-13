"""Collector（采集器）定义和审批契约。"""

from pathlib import PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class CollectorOutputContract(BaseModel):
    """采集器输出契约。"""

    model_config = ConfigDict(extra="forbid")

    schema_id: str = Field(min_length=1, max_length=128)
    media_type: str = Field(min_length=1, max_length=128)
    output_path: str = Field(min_length=1, max_length=255, pattern=r"^[A-Za-z0-9_./-]+$")

    @field_validator("output_path")
    @classmethod
    def validate_output_path(cls, value: str) -> str:
        """输出只能进入标准证据包目录，禁止路径穿越。"""

        path = PurePosixPath(value)
        allowed = {
            "logs",
            "metrics",
            "configs",
            "topology",
            "hardware",
            "changes",
            "tasks",
            "states",
            "commands",
            "exports",
            "attachments",
        }
        if path.is_absolute() or ".." in path.parts or len(path.parts) < 2 or path.parts[0] not in allowed:
            raise ValueError("output_path 必须位于标准证据包目录内且不得包含路径穿越")
        return value


class CollectorDefinitionWrite(BaseModel):
    """创建或更新 Collector 定义。"""

    model_config = ConfigDict(extra="forbid")

    collector_id: str = Field(min_length=2, max_length=128, pattern=r"^[a-z][a-z0-9_.-]+$")
    display_name: str = Field(min_length=1, max_length=255)
    description: str = Field(min_length=1, max_length=2000)
    platform: Literal["linux", "hci_api", "manual"] = "linux"
    executor: Literal["shell", "http", "manual"] = "shell"
    command_template: str = Field(min_length=1, max_length=4000)
    parameter_schema: dict[str, Any] = Field(default_factory=dict)
    risk_level: Literal["read_only"] = "read_only"
    timeout_seconds: int = Field(default=30, ge=1, le=3600)
    max_output_mb: float = Field(default=4, gt=0, le=4)
    supported_product_versions: list[str] = Field(min_length=1, max_length=64)
    output_contract: CollectorOutputContract
    version: str = Field(min_length=1, max_length=64)
    managed_by: Literal["manual", "kbd_sync"] = "manual"
    generation_metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_versions(self) -> "CollectorDefinitionWrite":
        """校验产品版本模式和平台执行器组合。"""

        if any(not pattern.strip() or len(pattern) > 64 for pattern in self.supported_product_versions):
            raise ValueError("supported_product_versions 每项长度必须为 1-64")
        expected_executor = {"linux": "shell", "hci_api": "http", "manual": "manual"}[self.platform]
        if self.executor != expected_executor:
            raise ValueError(f"platform={self.platform} 必须使用 executor={expected_executor}")
        return self


class CollectorApprovalRequest(BaseModel):
    """审批 Collector 请求。"""

    model_config = ConfigDict(extra="forbid")

    approved: bool = True
    reason: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def validate_reason(self) -> "CollectorApprovalRequest":
        """拒绝审批时必须给出原因。"""

        if not self.approved and not (self.reason or "").strip():
            raise ValueError("拒绝审批时必须填写 reason")
        return self


class CollectorDefinitionResponse(BaseModel):
    """Collector 当前事实源响应。"""

    collector_id: str
    display_name: str
    description: str
    platform: str
    executor: str
    command_template: str
    parameter_schema: dict[str, Any]
    risk_level: str
    timeout_seconds: int
    max_output_mb: float
    supported_product_versions: list[str]
    output_contract: dict[str, Any]
    version: str
    review_status: str
    is_enabled: bool
    approved_by: str | None
    approved_at: str | None
    rejection_reason: str | None
    lock_version: int
    managed_by: str = "manual"
    generation_metadata: dict[str, Any] = Field(default_factory=dict)
    active_revision: int | None = None
    active_checksum: str | None = None

    @classmethod
    def from_entity(cls, entity, *, snapshot=None) -> "CollectorDefinitionResponse":
        """将事实源和可选运行时快照转换为响应。"""

        return cls(
            collector_id=entity.collector_id,
            display_name=entity.display_name,
            description=entity.description,
            platform=entity.platform,
            executor=entity.executor,
            command_template=entity.command_template,
            parameter_schema=dict(entity.parameter_schema or {}),
            risk_level=entity.risk_level,
            timeout_seconds=entity.timeout_seconds,
            max_output_mb=float(entity.max_output_mb),
            supported_product_versions=list(entity.supported_product_versions or []),
            output_contract=dict(entity.output_contract or {}),
            version=entity.semantic_version,
            review_status=entity.review_status,
            is_enabled=bool(entity.is_enabled),
            approved_by=entity.approved_by,
            approved_at=entity.approved_at.isoformat() if entity.approved_at else None,
            rejection_reason=entity.rejection_reason,
            lock_version=entity.lock_version,
            managed_by=getattr(entity, "managed_by", "manual"),
            generation_metadata=dict(getattr(entity, "generation_metadata", None) or {}),
            active_revision=snapshot.revision if snapshot else None,
            active_checksum=snapshot.checksum if snapshot else None,
        )
