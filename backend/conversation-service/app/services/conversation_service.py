"""
Conversation Service - 对话业务逻辑层
"""

from typing import List, Optional, AsyncGenerator, Dict, Any
import uuid
import json

from ..models.conversation import Conversation
from ..models.message import Message, MessageRole
from ..repositories.conversation_repo import ConversationRepository
from .openclaw_client import OpenClawClient
from shared.utils.logger import get_logger

logger = get_logger("conversation-service")

class ConversationService:
    """对话业务服务"""
    
    def __init__(
        self, 
        repository: ConversationRepository,
        openclaw_client: OpenClawClient
    ):
        self.repository = repository
        self.openclaw = openclaw_client
        
    async def create_conversation(
        self,
        case_id: str,
        trace_id: str,
        initial_message: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> Conversation:
        """创建新对话"""
        conversation = await self.repository.create_conversation(
            case_id=case_id,
            trace_id=trace_id,
            metadata=metadata
        )
        
        logger.info(
            event="conversation_created",
            message=f"Created conversation {conversation.conversation_id}",
            case_id=case_id,
            conversation_id=str(conversation.conversation_id),
            trace_id=trace_id
        )
        
        # 如果有初始消息，立即发送该消息（但不等待回复，因为这是创建接口）
        # 注意：通常创建对话时如果有初始消息，应该调用 send_message 接口
        # 这里仅作记录，实际业务中前端通常会先调创建，再调发送
        
        return conversation

    async def get_conversation(self, conversation_id: uuid.UUID) -> Optional[Conversation]:
        """获取对话详情"""
        return await self.repository.get_conversation(conversation_id)

    async def get_messages(self, conversation_id: uuid.UUID) -> List[Message]:
        """获取对话历史"""
        return await self.repository.get_messages(conversation_id)
        
    async def send_message_stream_only(
        self,
        conversation_id: uuid.UUID,
        case_id: str,
        content: str,
        trace_id: str
    ) -> AsyncGenerator[str, None]:
        """
        发送消息并获取流式回复(仅负责调用流并yield)
        
        1. 保存用户消息
        2. 获取历史上下文
        3. 调用OpenClaw
        4. 流式返回响应
        """
        # 1. 保存用户消息
        await self.repository.add_message(
            conversation_id=conversation_id,
            case_id=case_id,
            role=MessageRole.user,
            content=content,
            trace_id=trace_id
        )
        
        # 2. 获取历史上下文 (最近20条)
        all_messages = await self.repository.get_messages(conversation_id)
        history_messages = []
        selected_messages = all_messages[-20:] if len(all_messages) > 20 else all_messages
        
        for msg in selected_messages:
            history_messages.append({
                "role": msg.role.value,
                "content": msg.content
            })
            
        # 3. 调用OpenClaw并流式返回
        try:
            async for chunk in self.openclaw.chat_completion_stream(
                messages=history_messages,
                user_id=f"case-{case_id}",  # 映射 Session Key
                trace_id=trace_id
            ):
                if chunk:
                    yield chunk
                    
        except Exception as e:
            import asyncio
            if isinstance(e, asyncio.CancelledError):
                logger.info(event="stream_cancelled", message="Stream was cancelled by client")
            else:
                logger.error(
                    event="conversation_error",
                    message="Error during AI generation",
                    conversation_id=str(conversation_id),
                    error=str(e),
                    trace_id=trace_id
                )
                yield f"\n[System Error: {str(e)}]"
                raise

    async def save_assistant_message(
        self,
        conversation_id: uuid.UUID,
        case_id: str,
        content: str,
        trace_id: str
    ) -> None:
        """保存AI返回的完整消息(后台执行)"""
        if not content:
            return
            
        try:
            await self.repository.add_message(
                conversation_id=conversation_id,
                case_id=case_id,
                role=MessageRole.assistant,
                content=content,
                trace_id=trace_id
            )
            logger.info(
                event="conversation_reply",
                message="AI response saved in background",
                conversation_id=str(conversation_id),
                response_length=len(content),
                trace_id=trace_id
            )
        except Exception as e:
            logger.error(
                event="conversation_save_error",
                message="Error saving AI response in background",
                conversation_id=str(conversation_id),
                error=str(e),
                trace_id=trace_id
            )
