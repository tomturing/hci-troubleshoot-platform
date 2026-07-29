"""KBD Proposal/Expert 最小不可变版本模型。

该表只保存知识生产和专家维护的 append-only 快照。Agent 的运行时发布、active
指针和使用审计继续复用 shared dynamic resource，避免重复建设生命周期表族。
"""

from __future__ import annotations

from shared.database.postgres import Base
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB


class KbdRevision(Base):
    """一条不可变的 LLM Proposal 或 Expert 快照。"""

    __tablename__ = "kbd_revision"
    __table_args__ = (
        CheckConstraint("revision_no > 0", name="chk_kbd_revision_revision_no"),
        CheckConstraint("revision_type IN ('proposal', 'expert')", name="chk_kbd_revision_type"),
        CheckConstraint(
            "actor_type IN ('llm', 'expert', 'migration', 'system')",
            name="chk_kbd_revision_actor_type",
        ),
        UniqueConstraint("kbd_entry_id", "revision_no", name="uq_kbd_revision_no"),
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    kbd_entry_id = Column(BigInteger, ForeignKey("kbd_entry.id", ondelete="RESTRICT"), nullable=False)
    revision_no = Column(Integer, nullable=False)
    revision_type = Column(String(16), nullable=False)
    parent_revision_id = Column(BigInteger, ForeignKey("kbd_revision.id", ondelete="RESTRICT"), nullable=True)
    payload_json = Column(JSONB, nullable=False)
    checksum = Column(String(64), nullable=False)
    generation_metadata = Column(JSONB, nullable=False, default=dict)
    validation_summary = Column(JSONB, nullable=False, default=dict)
    actor_id = Column(String(128), nullable=True)
    actor_type = Column(String(16), nullable=False)
    trace_id = Column(String(64), nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
