"""采集计划 ORM 模型。"""

import uuid

from shared.database.postgres import Base
from shared.models.base import TimestampMixin
from sqlalchemy import CheckConstraint, Column, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID


class CollectionPlan(Base, TimestampMixin):
    """由采集画像展开后形成的不可变采集计划。"""

    __tablename__ = "collection_plan"
    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_collection_plan_tenant_idempotency"),
        UniqueConstraint(
            "session_id",
            "plan_sequence",
            "plan_revision",
            name="uq_collection_plan_session_sequence_revision",
        ),
        CheckConstraint("plan_sequence BETWEEN 0 AND 1", name="ck_collection_plan_sequence"),
        CheckConstraint("plan_revision >= 1", name="ck_collection_plan_revision"),
        CheckConstraint("estimated_size_mb >= 0", name="ck_collection_plan_estimated_size"),
        CheckConstraint("estimated_duration_seconds >= 0", name="ck_collection_plan_estimated_duration"),
        CheckConstraint("status IN ('ready', 'superseded')", name="ck_collection_plan_status"),
    )

    plan_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id = Column(
        UUID(as_uuid=True),
        ForeignKey("diagnosis_session.session_id", ondelete="CASCADE"),
        nullable=False,
    )
    tenant_id = Column(String(128), nullable=False)
    created_by = Column(String(128), nullable=False)
    plan_sequence = Column(Integer, nullable=False, default=0)
    plan_revision = Column(Integer, nullable=False, default=1)
    profile_name = Column(String(128), nullable=False)
    profile_revision = Column(Integer, nullable=False)
    profile_version = Column(String(64), nullable=False)
    profile_checksum = Column(String(128), nullable=False)
    product_version = Column(String(64), nullable=False)
    kbd_ruleset_snapshot = Column(JSONB, nullable=False, default=list)
    kbd_ruleset_checksum = Column(String(64), nullable=False, default="")
    context_snapshot = Column(JSONB, nullable=False, default=dict)
    required_permissions = Column(JSONB, nullable=False, default=list)
    sensitive_data_types = Column(JSONB, nullable=False, default=list)
    unresolved_variables = Column(JSONB, nullable=False, default=list)
    estimated_size_mb = Column(Numeric(12, 2), nullable=False, default=0)
    estimated_duration_seconds = Column(Integer, nullable=False, default=0)
    status = Column(String(16), nullable=False, default="ready")
    idempotency_key = Column(String(128), nullable=False)
    request_hash = Column(String(64), nullable=False)
    trace_id = Column(String(64), nullable=False)


class CollectionPlanItem(Base):
    """采集计划中的单个执行项。"""

    __tablename__ = "collection_plan_item"
    __table_args__ = (
        UniqueConstraint("plan_id", "sequence", name="uq_collection_plan_item_sequence"),
        CheckConstraint("sequence >= 1", name="ck_collection_plan_item_sequence"),
        CheckConstraint("expected_size_mb >= 0", name="ck_collection_plan_item_expected_size"),
        CheckConstraint("timeout_seconds BETWEEN 1 AND 86400", name="ck_collection_plan_item_timeout"),
        CheckConstraint(
            "required_level IN ('mandatory', 'recommended', 'conditional', 'deep_dive')",
            name="ck_collection_plan_item_required_level",
        ),
        CheckConstraint(
            "activation_state IN ('active', 'deferred')",
            name="ck_collection_plan_item_activation_state",
        ),
    )

    item_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    plan_id = Column(
        UUID(as_uuid=True),
        ForeignKey("collection_plan.plan_id", ondelete="CASCADE"),
        nullable=False,
    )
    sequence = Column(Integer, nullable=False)
    collector_id = Column(String(128), nullable=False)
    collector_revision = Column(Integer, nullable=True)
    collector_version = Column(String(64), nullable=True)
    collector_checksum = Column(String(128), nullable=True)
    display_name = Column(String(255), nullable=False)
    required_level = Column(String(32), nullable=False)
    activation_state = Column(String(16), nullable=False, default="active")
    target = Column(JSONB, nullable=False, default=dict)
    time_window = Column(JSONB, nullable=False, default=dict)
    collector_parameters = Column(JSONB, nullable=False, default=dict)
    condition_snapshot = Column(JSONB, nullable=True)
    reason = Column(Text, nullable=False)
    expected_size_mb = Column(Numeric(12, 2), nullable=False, default=0)
    timeout_seconds = Column(Integer, nullable=False)
    required_permissions = Column(JSONB, nullable=False, default=list)
    sensitive_data_types = Column(JSONB, nullable=False, default=list)
    trace_id = Column(String(64), nullable=False)
