"""
Conversation Repository - 对话与消息数据访问层
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import desc, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.conversation import Conversation
from ..models.message import Message, MessageRole


class ConversationRepository:
    """对话数据访问层"""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def create_conversation(
        self, case_id: str, trace_id: str, assistant_type: str = "openclaw", metadata: dict[str, Any] | None = None
    ) -> Conversation:
        """创建新对话"""
        if metadata is None:
            metadata = {}

        conversation = Conversation(
            conversation_id=uuid.uuid4(),
            case_id=case_id,
            assistant_type=assistant_type,
            trace_id=trace_id,
            started_at=datetime.now(UTC),
            message_count=0,
            metadata_=metadata,
        )
        self.session.add(conversation)
        await self.session.flush()
        await self.session.refresh(conversation)
        return conversation

    async def get_conversation(self, conversation_id: uuid.UUID) -> Conversation | None:
        """查询对话"""
        result = await self.session.execute(select(Conversation).where(Conversation.conversation_id == conversation_id))
        return result.scalar_one_or_none()

    async def get_conversations_by_case(self, case_id: str) -> list[Conversation]:
        """查询工单的所有对话"""
        result = await self.session.execute(
            select(Conversation).where(Conversation.case_id == case_id).order_by(desc(Conversation.started_at))
        )
        return list(result.scalars().all())

    async def end_conversation(self, conversation_id: uuid.UUID) -> Conversation | None:
        """结束对话"""
        conversation = await self.get_conversation(conversation_id)
        if conversation:
            conversation.ended_at = datetime.now(UTC)
            await self.session.flush()
            await self.session.refresh(conversation)
        return conversation

    async def add_message(
        self,
        conversation_id: uuid.UUID,
        case_id: str,
        role: MessageRole,
        content: str,
        trace_id: str,
        command: str | None = None,
        command_warning: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Message:
        """添加消息"""
        if metadata is None:
            metadata = {}

        message = Message(
            message_id=uuid.uuid4(),
            conversation_id=conversation_id,
            case_id=case_id,
            role=role,
            content=content,
            command=command,
            command_warning=command_warning,
            trace_id=trace_id,
            created_at=datetime.now(UTC),
            metadata_=metadata,
        )
        self.session.add(message)

        # 注意: 不手动更新 message_count，数据库触发器 update_conversation_message_count 会自动处理
        # 注意: 不手动调用 commit()，DatabaseManager.get_session() 上下文管理器在退出时会自动提交
        await self.session.flush()
        await self.session.refresh(message)
        return message

    async def get_messages(self, conversation_id: uuid.UUID) -> list[Message]:
        """获取对话的所有消息"""
        result = await self.session.execute(
            select(Message).where(Message.conversation_id == conversation_id).order_by(Message.created_at.asc())
        )
        return list(result.scalars().all())

    async def get_recent_user_messages(
        self,
        case_id: str,
        current_message_id: uuid.UUID,
        limit: int = 10,
    ) -> list[Message]:
        """
        获取当前 case 下最近 N 条用户消息（排除当前消息）

        用于重复提问检测，仅获取用户消息，按时间倒序排列
        """
        result = await self.session.execute(
            select(Message)
            .where(Message.case_id == case_id)
            .where(Message.message_id != current_message_id)
            .where(Message.role == MessageRole.user)
            .order_by(desc(Message.created_at))
            .limit(limit)
        )
        return list(result.scalars().all())

    async def increment_repeat_question_count(self, conversation_id: uuid.UUID) -> None:
        """
        增加重复提问计数

        UPDATE conversation SET repeat_question_count = repeat_question_count + 1 WHERE id = {conversation_id}
        """
        await self.session.execute(
            update(Conversation)
            .where(Conversation.conversation_id == conversation_id)
            .values(repeat_question_count=Conversation.repeat_question_count + 1)
        )

    async def insert_prompt_audit(
        self,
        conversation_id: uuid.UUID,
        case_id: str,
        assistant_type: str,
        trace_id: str,
        message_count: int,
        has_sop: bool,
        kb_chunks_count: int,
        kb_top_score: float | None,
        messages: list | None,
    ) -> None:
        """向 prompt_audit 表插入一条审计记录"""
        import json as _json

        await self.session.execute(
            text("""
                INSERT INTO prompt_audit (
                    conversation_id, case_id, assistant_type, trace_id,
                    message_count, has_sop, kb_chunks_count, kb_top_score,
                    messages
                ) VALUES (
                    :conversation_id, :case_id, :assistant_type, :trace_id,
                    :message_count, :has_sop, :kb_chunks_count, :kb_top_score,
                    :messages
                )
            """),
            {
                "conversation_id": str(conversation_id),
                "case_id": case_id,
                "assistant_type": assistant_type,
                "trace_id": trace_id,
                "message_count": message_count,
                "has_sop": has_sop,
                "kb_chunks_count": kb_chunks_count,
                "kb_top_score": kb_top_score,
                "messages": _json.dumps(messages, ensure_ascii=False) if messages else None,
            },
        )
