"""
Conversation Repository - 对话与消息数据访问层
"""

from typing import List, Optional, Dict, Any
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import datetime, timezone
import uuid

from ..models.conversation import Conversation
from ..models.message import Message, MessageRole


class ConversationRepository:
    """对话数据访问层"""
    
    def __init__(self, session: AsyncSession):
        self.session = session
    
    async def create_conversation(
        self, 
        case_id: str, 
        trace_id: str,
        assistant_type: str = "openclaw",
        metadata: Optional[Dict[str, Any]] = None
    ) -> Conversation:
        """创建新对话"""
        if metadata is None:
            metadata = {}
            
        conversation = Conversation(
            conversation_id=uuid.uuid4(),
            case_id=case_id,
            assistant_type=assistant_type,
            trace_id=trace_id,
            started_at=datetime.now(timezone.utc),
            message_count=0,
            metadata_=metadata
        )
        self.session.add(conversation)
        await self.session.flush()
        await self.session.refresh(conversation)
        return conversation
    
    async def get_conversation(self, conversation_id: uuid.UUID) -> Optional[Conversation]:
        """查询对话"""
        result = await self.session.execute(
            select(Conversation).where(Conversation.conversation_id == conversation_id)
        )
        return result.scalar_one_or_none()
    
    async def get_conversations_by_case(self, case_id: str) -> List[Conversation]:
        """查询工单的所有对话"""
        result = await self.session.execute(
            select(Conversation)
            .where(Conversation.case_id == case_id)
            .order_by(desc(Conversation.started_at))
        )
        return list(result.scalars().all())
    
    async def end_conversation(self, conversation_id: uuid.UUID) -> Optional[Conversation]:
        """结束对话"""
        conversation = await self.get_conversation(conversation_id)
        if conversation:
            conversation.ended_at = datetime.now(timezone.utc)
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
        command: Optional[str] = None,
        command_warning: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
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
            created_at=datetime.now(timezone.utc),
            metadata_=metadata
        )
        self.session.add(message)
        
        # 更新对话的消息计数
        conversation = await self.get_conversation(conversation_id)
        if conversation:
            conversation.message_count += 1
            
        await self.session.commit()
        await self.session.refresh(message)
        return message
    
    async def get_messages(self, conversation_id: uuid.UUID) -> List[Message]:
        """获取对话的所有消息"""
        result = await self.session.execute(
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.created_at.asc())
        )
        return list(result.scalars().all())
