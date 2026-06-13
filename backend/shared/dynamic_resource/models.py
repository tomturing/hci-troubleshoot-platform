"""动态资源运行时数据结构。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class ResourceKey:
    """动态资源唯一键。"""

    resource_type: str
    resource_name: str

    def cache_key(self) -> tuple[str, str]:
        return (self.resource_type, self.resource_name)


@dataclass(frozen=True)
class ResourceSnapshot:
    """动态资源运行时快照。"""

    resource_type: str
    resource_name: str
    revision: int
    version: str
    status: str
    content: dict[str, Any]
    contract: dict[str, Any]
    dependencies: list[dict[str, Any]]
    checksum: str
    trace_id: str | None = None
    published_at: datetime | None = None


@dataclass(frozen=True)
class UsageRecord:
    """动态资源使用审计输入。"""

    consumer: str
    status: str
    conversation_id: str | None = None
    case_id: str | None = None
    trace_id: str | None = None
    exec_id: str | None = None
    input_payload: Any | None = None
    output_payload: Any | None = None
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ValidationIssue:
    """动态资源校验问题。"""

    level: str
    location: str
    message: str
    code: str

    def to_dict(self) -> dict[str, str]:
        return {
            "level": self.level,
            "location": self.location,
            "message": self.message,
            "code": self.code,
        }


@dataclass(frozen=True)
class ValidationResult:
    """动态资源校验结果。"""

    status: str
    issues: list[ValidationIssue] = field(default_factory=list)

    @classmethod
    def ok(cls) -> ValidationResult:
        return cls(status="ok", issues=[])

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "validation_issues": [issue.to_dict() for issue in self.issues],
        }
