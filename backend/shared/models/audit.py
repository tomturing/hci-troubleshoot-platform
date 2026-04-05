"""
审计 ORM 模型（v6.2 重构）

v6.2 变更说明：
  - [废弃] ToolAuditLog：原指向 tool_audit_log 表，该表已被 migration 20260402003 删除！
  - [新增] ToolResult：指向 tool_result 表，解 BUG-03（step_no 缺失）和 BUG（表不存在）
  - [新增] AuditLog：指向 audit_log 表（精简后仅 prompt 类型），用于 Prompt 构建审计
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, SmallInteger, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID

from ..database.postgres import Base


class ToolResult(Base):
    """
    工具执行记录模型

    对应数据库表：tool_result
    替代已删除的 tool_audit_log 表，新增 step_no 字段（BUG-03 修复）。
    由 AuditService.write() 写入，ReactExecutor 每次工具调用后触发。
    """

    __tablename__ = "tool_result"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))

    # 会话关联（UUID 类型，与 conversation.conversation_id 一致）
    conversation_id = Column(
        UUID(as_uuid=True),
        ForeignKey("conversation.conversation_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # 工具信息
    tool_name = Column(String(100), nullable=False, index=True)   # 工具标识，对应 tool_definition.tool_name
    tool_type = Column(String(20), nullable=False)                # acli / scp_api

    # BUG-03 修复：步骤编号，对应 diagnostic_item 中 type=verification_step 的 seq
    step_no = Column(SmallInteger, nullable=True)

    # 风险控制
    risk_level = Column(SmallInteger, nullable=False, default=1)  # 1=只读 2=写操作 3=高危
    policy = Column(String(20), nullable=False)                   # auto|notify|confirm|block
    authorized_by = Column(String(100), nullable=True)            # policy=confirm 时的授权用户标识

    # 执行结果
    input_json = Column(JSONB, nullable=False, default=dict)      # 工具调用输入参数
    output_json = Column(JSONB, nullable=True)                    # 工具执行结果
    error = Column(Text, nullable=True)                           # 执行异常信息

    # 时间统计
    started_at = Column(DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    duration_ms = Column(Integer, nullable=True)                  # 执行耗时（毫秒）

    # 链路追踪
    trace_id = Column(String(64), nullable=True, index=True)

    def __repr__(self) -> str:
        return (
            f"<ToolResult(id={self.id}, tool={self.tool_name!r}, "
            f"conversation={self.conversation_id}, risk={self.risk_level}, step={self.step_no})>"
        )


class AuditLog(Base):
    """
    Prompt 构建审计模型（v6.2 精简）

    对应数据库表：audit_log
    v6.2 后仅记录 Prompt 构建（audit_type='prompt'），工具执行记录已迁移至 tool_result。
    system_prompt_id 关联本次使用的模板版本，用于效果追踪和快速回滚。
    """

    __tablename__ = "audit_log"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    audit_type = Column(String(20), nullable=False, default="prompt")  # 固定为 prompt（tool_call 已迁移）

    # 会话关联
    conversation_id = Column(
        UUID(as_uuid=True),
        ForeignKey("conversation.conversation_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    turn_index = Column(SmallInteger, nullable=True)                   # 对话轮次索引

    # Prompt 模板关联（v6.2 新增）
    system_prompt_id = Column(
        Integer,
        ForeignKey("system_prompt.id", ondelete="SET NULL"),
        nullable=True,
    )

    # 类型专属字段（存入 payload JSONB）
    payload = Column(JSONB, nullable=False, default=dict)              # {case_id, model, messages, token_count, ...}

    # 执行状态
    error = Column(Text, nullable=True)
    duration_ms = Column(Integer, nullable=True)
    started_at = Column(DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    trace_id = Column(String(64), nullable=True, index=True)

    def __repr__(self) -> str:
        return (
            f"<AuditLog(id={self.id}, type={self.audit_type!r}, "
            f"conversation={self.conversation_id}, turn={self.turn_index})>"
        )


# 向后兼容别名（过渡期使用，待 audit_service.py 完成重构后删除）
ToolAuditLog = ToolResult
