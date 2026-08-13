"""安全采集器事实源模型。"""

from shared.database.postgres import Base
from shared.models.base import TimestampMixin
from sqlalchemy import Boolean, CheckConstraint, Column, DateTime, Integer, Numeric, String, Text
from sqlalchemy.dialects.postgresql import JSONB


class CollectorDefinition(Base, TimestampMixin):
    """Collector（采集器）当前编辑态和审批状态。"""

    __tablename__ = "collector_definition"
    __table_args__ = (
        CheckConstraint("lock_version >= 1", name="ck_collector_definition_lock_version"),
        CheckConstraint("timeout_seconds BETWEEN 1 AND 3600", name="ck_collector_definition_timeout"),
        CheckConstraint("max_output_mb > 0 AND max_output_mb <= 4", name="ck_collector_definition_output_size"),
        CheckConstraint("platform IN ('linux', 'hci_api', 'manual')", name="ck_collector_definition_platform"),
        CheckConstraint("executor IN ('shell', 'http', 'manual')", name="ck_collector_definition_executor"),
        CheckConstraint("risk_level = 'read_only'", name="ck_collector_definition_risk"),
        CheckConstraint(
            "review_status IN ('draft', 'approved', 'rejected')",
            name="ck_collector_definition_review_status",
        ),
        CheckConstraint("managed_by IN ('manual', 'kbd_sync')", name="ck_collector_definition_managed_by"),
    )

    collector_id = Column(String(128), primary_key=True)
    display_name = Column(String(255), nullable=False)
    description = Column(Text, nullable=False)
    platform = Column(String(32), nullable=False)
    executor = Column(String(32), nullable=False)
    command_template = Column(Text, nullable=False)
    parameter_schema = Column(JSONB, nullable=False, default=dict)
    risk_level = Column(String(32), nullable=False, default="read_only")
    timeout_seconds = Column(Integer, nullable=False)
    max_output_mb = Column(Numeric(10, 2), nullable=False)
    supported_product_versions = Column(JSONB, nullable=False, default=list)
    output_contract = Column(JSONB, nullable=False, default=dict)
    managed_by = Column(String(32), nullable=False, default="manual")
    generation_metadata = Column(JSONB, nullable=False, default=dict)
    semantic_version = Column(String(64), nullable=False)
    review_status = Column(String(16), nullable=False, default="draft")
    is_enabled = Column(Boolean, nullable=False, default=False)
    approved_by = Column(String(128), nullable=True)
    approved_at = Column(DateTime(timezone=True), nullable=True)
    rejection_reason = Column(Text, nullable=True)
    lock_version = Column(Integer, nullable=False, default=1)
    trace_id = Column(String(64), nullable=False)
