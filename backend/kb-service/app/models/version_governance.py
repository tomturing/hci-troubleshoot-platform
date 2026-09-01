"""KBD 与仿真资产统一版本治理模型。

这些模型只保存不可变 digest、引用和工作区指针，不把大对象或可变运行状态混入快照。
"""

from __future__ import annotations

from datetime import UTC, datetime

from shared.database.postgres import Base
from sqlalchemy import BigInteger, Column, DateTime, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID


class KbdPackage(Base):
    """以 support_id 为唯一键的 KBD 工作区聚合根。"""

    __tablename__ = "kbd_package"

    package_id = Column(PGUUID(as_uuid=True), primary_key=True)
    support_id = Column(String(20), nullable=False, unique=True)
    working_snapshot_digest = Column(String(71), ForeignKey("package_snapshot.package_snapshot_digest"), nullable=True)
    active_release_id = Column(Integer, ForeignKey("dynamic_resource_revision.id"), nullable=True)
    workspace_version = Column(BigInteger, nullable=False, default=1)
    status = Column(String(20), nullable=False, default="draft_editing")
    trace_id = Column(String(64), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC))
    updated_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC))


class VerificationAsset(Base):
    """单次试运行产生的不可变验证凭证。"""

    __tablename__ = "verification_asset"

    asset_id = Column(PGUUID(as_uuid=True), primary_key=True)
    asset_digest = Column(String(71), nullable=False, unique=True)
    support_id = Column(String(20), nullable=False, index=True)
    signal_id = Column(String(128), nullable=False)
    processing_index = Column(Integer, nullable=False)
    dataset_id = Column(String(128), nullable=False)
    input_digest = Column(String(71), nullable=False)
    deterministic_input = Column(JSONB, nullable=False, default=dict)
    ai_input = Column(JSONB, nullable=False, default=dict)
    raw_response_hash = Column(String(128), nullable=True)
    output_json = Column(JSONB, nullable=False, default=dict)
    evidence_json = Column(JSONB, nullable=False, default=dict)
    downstream_result = Column(JSONB, nullable=False, default=dict)
    model = Column(String(128), nullable=False)
    prompt_revision = Column(String(128), nullable=False)
    contract_version = Column(String(128), nullable=False)
    run_id = Column(String(128), nullable=True)
    trace_id = Column(String(64), nullable=False, index=True)
    result_status = Column(String(20), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC))


class PackageSnapshot(Base):
    """一次完整业务视图的不可变 manifest。"""

    __tablename__ = "package_snapshot"

    package_snapshot_id = Column(PGUUID(as_uuid=True), primary_key=True)
    package_snapshot_digest = Column(String(71), nullable=False, unique=True)
    support_id = Column(String(20), nullable=False, index=True)
    parent_snapshot_digest = Column(
        String(71),
        ForeignKey("package_snapshot.package_snapshot_digest", ondelete="RESTRICT"),
        nullable=True,
    )
    knowledge_snapshot_digest = Column(String(71), nullable=False)
    signal_spec_digest = Column(String(71), nullable=False)
    simulation_spec_digest = Column(String(71), nullable=False)
    verification_assets = Column(JSONB, nullable=False, default=list)
    prompt_revision = Column(String(128), nullable=False)
    tool_contract_revision = Column(String(128), nullable=False)
    policy_revision = Column(String(128), nullable=False)
    compiler_revision = Column(String(128), nullable=False)
    manifest_json = Column(JSONB, nullable=False, default=dict)
    created_by = Column(String(128), nullable=False)
    trace_id = Column(String(64), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC))
