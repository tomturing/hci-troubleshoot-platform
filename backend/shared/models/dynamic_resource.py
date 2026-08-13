"""
动态资源运行时模型。

KBD/SOP/Tool/Skill/Prompt/Collection Profile 等动态资源共享 revision、active 指针和使用审计，
业务表仍作为管理页面事实源，本表负责运行时不可变快照和追踪。
"""

from datetime import UTC, datetime

from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB

from ..database.postgres import Base


class DynamicResourceRevision(Base):
    """动态资源不可变版本快照。"""

    __tablename__ = "dynamic_resource_revision"
    __table_args__ = (
        UniqueConstraint("resource_type", "resource_name", "revision", name="uq_dynamic_resource_revision"),
        UniqueConstraint("resource_type", "resource_name", "checksum", name="uq_dynamic_resource_checksum"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    resource_type = Column(String(32), nullable=False, index=True)
    resource_name = Column(String(128), nullable=False, index=True)
    version = Column(String(64), nullable=False, default="1.0")
    revision = Column(Integer, nullable=False)
    status = Column(String(16), nullable=False, default="published")
    content_json = Column(JSONB, nullable=False, default=dict)
    contract_json = Column(JSONB, nullable=False, default=dict)
    dependency_json = Column(JSONB, nullable=False, default=list)
    checksum = Column(String(128), nullable=False)
    trace_id = Column(String(64), nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False)
    published_at = Column(DateTime(timezone=True), nullable=True)


class DynamicResourceActive(Base):
    """动态资源当前激活 revision 指针。"""

    __tablename__ = "dynamic_resource_active"

    resource_type = Column(String(32), primary_key=True)
    resource_name = Column(String(128), primary_key=True)
    active_revision = Column(Integer, nullable=False)
    checksum = Column(String(128), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )
    trace_id = Column(String(64), nullable=True, index=True)


class DynamicResourceUsageAudit(Base):
    """动态资源使用审计。"""

    __tablename__ = "dynamic_resource_usage_audit"

    id = Column(Integer, primary_key=True, autoincrement=True)
    resource_type = Column(String(32), nullable=False, index=True)
    resource_name = Column(String(128), nullable=False, index=True)
    revision = Column(Integer, nullable=False)
    consumer = Column(String(128), nullable=False)
    conversation_id = Column(String(64), nullable=True, index=True)
    case_id = Column(String(64), nullable=True, index=True)
    trace_id = Column(String(64), nullable=True, index=True)
    exec_id = Column(String(64), nullable=True, index=True)
    input_hash = Column(String(128), nullable=True)
    output_hash = Column(String(128), nullable=True)
    status = Column(String(32), nullable=False)
    error = Column(Text, nullable=True)
    metadata_json = Column(JSONB, nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False)


class PromptSlot(Base):
    """Prompt 编排槽位，解耦代码中的逻辑槽位与具体 prompt name。"""

    __tablename__ = "prompt_slot"

    slot_name = Column(String(100), primary_key=True)
    active_prompt_name = Column(String(100), nullable=False)
    expected_placeholders = Column(JSONB, nullable=False, default=list)
    consumer = Column(String(128), nullable=False, default="agent-service")
    is_active = Column(Boolean, nullable=False, default=True)
    trace_id = Column(String(64), nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )
