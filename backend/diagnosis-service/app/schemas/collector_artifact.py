"""Collector Artifact（采集器制品）契约。"""

import json
from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class CollectorArtifactGenerateRequest(BaseModel):
    """生成无 Shell 的结构化采集器制品请求。"""

    model_config = ConfigDict(extra="forbid")

    collection_plan_id: UUID
    target_node: str | None = Field(default=None, max_length=255, pattern=r"^[A-Za-z0-9._:@-]+$")
    parameters_by_item: dict[str, dict[str, Any]] = Field(default_factory=dict)
    expires_in_hours: int = Field(default=24, ge=1, le=168)


class CollectorArtifactRevokeRequest(BaseModel):
    """撤销采集器制品请求。"""

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(
        default="platform_revoked",
        min_length=1,
        max_length=64,
        pattern=r"^[a-z][a-z0-9_]*$",
        description="不含客户敏感信息的机器可读撤销原因码",
    )


class CollectorArtifactItemResponse(BaseModel):
    """制品内 Collector 修订版本响应。"""

    plan_item_id: str
    sequence: int
    collector_id: str
    collector_revision: int
    collector_checksum: str
    rendered_command: str
    execution_spec: dict[str, Any]
    output_contract: dict[str, Any]
    timeout_seconds: int
    max_output_bytes: int


class CollectorArtifactResponse(BaseModel):
    """签名采集器制品响应。"""

    artifact_id: str
    session_id: str
    collection_plan_id: str
    target_key: str
    artifact_type: str
    schema_version: str
    file_name: str
    download_path: str
    verification_bundle_path: str
    artifact_sha256: str
    signature_algorithm: str
    signature_base64: str
    signing_key_id: str
    public_key_base64: str
    public_key_fingerprint: str
    signed_at: datetime
    expires_at: datetime
    status: str
    revoked_at: datetime | None
    revoked_by: str | None
    revocation_reason: str | None
    manifest: dict[str, Any]
    trace_id: str
    created_at: datetime
    items: list[CollectorArtifactItemResponse]

    @classmethod
    def from_entities(cls, artifact, items) -> "CollectorArtifactResponse":
        """将制品和制品项转换为响应。"""

        execution_specs = _load_execution_specs(artifact.content_text)

        return cls(
            artifact_id=str(artifact.artifact_id),
            session_id=str(artifact.session_id),
            collection_plan_id=str(artifact.collection_plan_id),
            target_key=artifact.target_key,
            artifact_type=artifact.artifact_type,
            schema_version=artifact.schema_version,
            file_name=artifact.file_name,
            download_path=(
                f"/api/diagnosis-sessions/{artifact.session_id}/collector-artifacts/{artifact.artifact_id}/download"
            ),
            verification_bundle_path=(
                f"/api/diagnosis-sessions/{artifact.session_id}/collector-artifacts/"
                f"{artifact.artifact_id}/verification-bundle"
            ),
            artifact_sha256=artifact.artifact_sha256,
            signature_algorithm=artifact.signature_algorithm,
            signature_base64=artifact.signature_base64,
            signing_key_id=artifact.signing_key_id,
            public_key_base64=artifact.public_key_base64,
            public_key_fingerprint=artifact.public_key_fingerprint,
            signed_at=artifact.signed_at,
            expires_at=artifact.expires_at,
            status=artifact.status,
            revoked_at=artifact.revoked_at,
            revoked_by=artifact.revoked_by,
            revocation_reason=artifact.revocation_reason,
            manifest=dict(artifact.manifest_json or {}),
            trace_id=artifact.trace_id,
            created_at=artifact.created_at,
            items=[
                CollectorArtifactItemResponse(
                    plan_item_id=str(item.plan_item_id),
                    sequence=item.sequence,
                    collector_id=item.collector_id,
                    collector_revision=item.collector_revision,
                    collector_checksum=item.collector_checksum,
                    rendered_command=item.rendered_command,
                    execution_spec=execution_specs.get(str(item.plan_item_id), {}),
                    output_contract=dict(item.output_contract or {}),
                    timeout_seconds=item.timeout_seconds,
                    max_output_bytes=item.max_output_bytes,
                )
                for item in items
            ],
        )


def _load_execution_specs(content_text: str) -> dict[str, dict[str, Any]]:
    """从签名制品正文提取允许向操作者展示的不可变执行规范。"""

    try:
        document = json.loads(content_text)
    except (TypeError, json.JSONDecodeError):
        return {}
    raw_items = document.get("execution_items") if isinstance(document, dict) else None
    if not isinstance(raw_items, list):
        return {}
    specs: dict[str, dict[str, Any]] = {}
    for raw_item in raw_items:
        if not isinstance(raw_item, dict) or not raw_item.get("item_id"):
            continue
        executor = raw_item.get("executor")
        spec: dict[str, Any] = {"executor": executor}
        if executor == "command" and isinstance(raw_item.get("argv"), list):
            spec["argv"] = [str(value) for value in raw_item["argv"]]
        elif executor == "http":
            spec.update({"method": raw_item.get("method"), "path": raw_item.get("path")})
        elif executor == "manual":
            spec["guide"] = raw_item.get("guide")
        specs[str(raw_item["item_id"])] = spec
    return specs
