"""采集器制品及其解析快照模型。"""

import uuid

from shared.database.postgres import Base
from shared.models.base import TimestampMixin
from sqlalchemy import CheckConstraint, Column, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID


class CollectorArtifact(Base, TimestampMixin):
    """已签名、可下载的结构化采集器制品。"""

    __tablename__ = "collector_artifact"
    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_collector_artifact_tenant_idempotency"),
        UniqueConstraint("collection_plan_id", "target_key", name="uq_collector_artifact_plan_target"),
        CheckConstraint("status IN ('ready', 'expired', 'revoked')", name="ck_collector_artifact_status"),
        CheckConstraint("expires_at > signed_at", name="ck_collector_artifact_expiry"),
        CheckConstraint(
            "revocation_reason IS NULL OR revocation_reason ~ '^[a-z][a-z0-9_]{0,63}$'",
            name="ck_collector_artifact_revocation_reason",
        ),
    )

    artifact_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id = Column(
        UUID(as_uuid=True),
        ForeignKey("diagnosis_session.session_id", ondelete="CASCADE"),
        nullable=False,
    )
    collection_plan_id = Column(
        UUID(as_uuid=True),
        ForeignKey("collection_plan.plan_id", ondelete="RESTRICT"),
        nullable=False,
    )
    tenant_id = Column(String(128), nullable=False)
    created_by = Column(String(128), nullable=False)
    target_key = Column(String(255), nullable=False, default="all")
    artifact_type = Column(String(32), nullable=False, default="structured_collector")
    schema_version = Column(String(32), nullable=False, default="1.2")
    file_name = Column(String(255), nullable=False)
    content_text = Column(Text, nullable=False)
    artifact_sha256 = Column(String(64), nullable=False)
    signature_algorithm = Column(String(32), nullable=False)
    signature_base64 = Column(Text, nullable=False)
    signing_key_id = Column(String(128), nullable=False)
    public_key_base64 = Column(Text, nullable=False)
    public_key_fingerprint = Column(String(64), nullable=False)
    signed_at = Column(DateTime(timezone=True), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    manifest_json = Column(JSONB, nullable=False, default=dict)
    status = Column(String(16), nullable=False, default="ready")
    revoked_at = Column(DateTime(timezone=True))
    revoked_by = Column(String(128))
    revocation_reason = Column(String(500))
    revoked_trace_id = Column(String(64))
    idempotency_key = Column(String(128), nullable=False)
    request_hash = Column(String(64), nullable=False)
    trace_id = Column(String(64), nullable=False)


class CollectorArtifactItem(Base):
    """制品内每个计划项对应的 Collector 修订版本快照。"""

    __tablename__ = "collector_artifact_item"
    __table_args__ = (
        UniqueConstraint("artifact_id", "sequence", name="uq_collector_artifact_item_sequence"),
        CheckConstraint("sequence >= 1", name="ck_collector_artifact_item_sequence"),
    )

    item_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    artifact_id = Column(
        UUID(as_uuid=True),
        ForeignKey("collector_artifact.artifact_id", ondelete="CASCADE"),
        nullable=False,
    )
    plan_item_id = Column(
        UUID(as_uuid=True),
        ForeignKey("collection_plan_item.item_id", ondelete="RESTRICT"),
        nullable=False,
    )
    sequence = Column(Integer, nullable=False)
    collector_id = Column(String(128), nullable=False)
    collector_revision = Column(Integer, nullable=False)
    collector_checksum = Column(String(128), nullable=False)
    rendered_command = Column(Text, nullable=False)
    output_contract = Column(JSONB, nullable=False, default=dict)
    timeout_seconds = Column(Integer, nullable=False)
    max_output_bytes = Column(Integer, nullable=False)
    trace_id = Column(String(64), nullable=False)
