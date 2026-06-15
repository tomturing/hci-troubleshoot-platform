"""
Message Model - 消息表
"""

import enum
import uuid
from datetime import UTC, datetime

from shared.database.postgres import Base
from shared.models.base import TraceableMixin
from sqlalchemy import Column, DateTime, ForeignKey, String, Text
from sqlalchemy import Enum as SQLEnum
from sqlalchemy.dialects.postgresql import JSONB, UUID


class MessageRole(enum.StrEnum):
    """消息角色枚举"""

    user = "user"
    assistant = "assistant"
    system = "system"
    command = "command"
    # ReAct 工具调用轮次角色（用于跨轮次持久化 ReAct 历史）
    tool_call = "tool_call"    # AI 发起的工具调用请求（含 tool_calls JSON）
    tool_result = "tool_result"  # 工具执行结果（通过 tool_call_id 关联对应的 tool_call 消息）


class Message(Base, TraceableMixin):
    """消息表"""

    __tablename__ = "message"

    message_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conversation_id = Column(
        UUID(as_uuid=True), ForeignKey("conversation.conversation_id", ondelete="CASCADE"), nullable=False, index=True
    )
    case_id = Column(String(20), nullable=False, index=True)
    role = Column(SQLEnum(MessageRole, name="message_role"), nullable=False)
    content = Column(Text, nullable=False)
    command = Column(Text, nullable=True)
    command_warning = Column(Text, nullable=True)
    metadata_ = Column("metadata", JSONB, default=dict)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False)
    # ReAct 工具调用关联字段：role=tool_result 时关联对应的 tool_call 请求 ID
    tool_call_id = Column(Text, nullable=True)

    def __repr__(self):
        return f"<Message(message_id={self.message_id}, role={self.role}, case_id={self.case_id})>"
