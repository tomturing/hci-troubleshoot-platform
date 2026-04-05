"""
DiagnosticItem Model - 诊断结论子表

与 Message 完全同构的子实体设计（1 conversation → N diagnostic_item）。
存储 S2-S5 各阶段产生的结构化结论：
  - S2: hypothesis（根因假设列表）
  - S3: verification_step（验证步骤列表）
  - S4: root_cause（根因结论，通常 1 条）
  - S5: solution（解决方案，1-2 条）

解决 BUG-06：
  原 conversation.hypothesis JSONB blob 导致：
    1. 并发竞态（更新一条假设需读-改-写整个数组）
    2. 无独立时间戳（无法知道每条假设何时生成/排除）
    3. 无独立查询（无法过滤 status=rejected 的假设）
    4. Pod 重启假设全丢（BUG-06 根因：DiagnosticSession 是纯内存 Pydantic 对象）

status 值域（v6.3 补充 archived）：
  pending      - 待处理（刚生成）
  in_progress  - 验证中（S3 阶段正在验证该假设）
  confirmed    - 已确认/验证通过
  rejected     - 已排除/验证失败
  skipped      - 跳过
  archived     - 已归档：S6 用户选 B 重进 S1 时，旧诊断周期所有条目批量设置为此状态
"""

import uuid
from datetime import UTC, datetime

from shared.database.postgres import Base
from shared.models.base import TraceableMixin
from sqlalchemy import Column, DateTime, Float, SmallInteger, String
from sqlalchemy import ForeignKey
from sqlalchemy.dialects.postgresql import JSONB, UUID

# status 值域常量（避免硬编码字符串）
STATUS_PENDING = "pending"
STATUS_IN_PROGRESS = "in_progress"
STATUS_CONFIRMED = "confirmed"
STATUS_REJECTED = "rejected"
STATUS_SKIPPED = "skipped"
STATUS_ARCHIVED = "archived"  # S6 用户选 B 时批量设置


class DiagnosticItem(Base, TraceableMixin):
    """诊断结论子表"""

    __tablename__ = "diagnostic_item"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conversation_id = Column(
        UUID(as_uuid=True),
        ForeignKey("conversation.conversation_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    stage = Column(String(5), nullable=False)                        # S2/S3/S4/S5
    type = Column(String(30), nullable=False)                        # hypothesis/verification_step/root_cause/solution
    seq = Column(SmallInteger, nullable=False, default=1)            # 同会话同类型内排序序号（从1开始）
    content = Column(JSONB, nullable=False, default=dict)            # 结构化内容，按 type 格式不同
    probability = Column(Float, nullable=True)                       # 假设概率 0.0-1.0，仅 type=hypothesis 有值
    status = Column(String(20), nullable=False, default="pending")  # pending/in_progress/confirmed/rejected/skipped
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )

    def __repr__(self):
        return (
            f"<DiagnosticItem(id={self.id}, conversation_id={self.conversation_id}, "
            f"type={self.type!r}, stage={self.stage!r}, status={self.status!r})>"
        )
