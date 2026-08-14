"""Collection Profile（采集画像）事实源模型。"""

from shared.database.postgres import Base
from shared.models.base import TimestampMixin
from sqlalchemy import Boolean, CheckConstraint, Column, DateTime, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB


class CollectionProfileDefinitionEntity(Base, TimestampMixin):
    """采集画像当前编辑态、审批态和启用状态。"""

    __tablename__ = "collection_profile_definition"
    __table_args__ = (
        CheckConstraint("lock_version >= 1", name="ck_collection_profile_definition_lock_version"),
        CheckConstraint(
            "review_status IN ('draft', 'approved', 'rejected')",
            name="ck_collection_profile_definition_review_status",
        ),
        CheckConstraint("managed_by IN ('manual', 'kbd_sync')", name="ck_collection_profile_definition_managed_by"),
    )

    profile_id = Column(String(128), primary_key=True)
    profile_json = Column(JSONB, nullable=False, default=dict)
    managed_by = Column(String(32), nullable=False, default="manual")
    generation_metadata = Column(JSONB, nullable=False, default=dict)
    semantic_version = Column(String(64), nullable=False)
    review_status = Column(String(16), nullable=False, default="draft")
    is_enabled = Column(Boolean, nullable=False, default=False)
    approved_by = Column(String(128))
    approved_at = Column(DateTime(timezone=True))
    rejection_reason = Column(Text)
    lock_version = Column(Integer, nullable=False, default=1)
    trace_id = Column(String(64), nullable=False)
