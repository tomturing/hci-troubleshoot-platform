"""
Conversation Service - 对话业务逻辑层 (v2.0 多类型AI助手)
"""

from typing import List, Optional, AsyncGenerator, Dict, Any
import uuid
import json

from ..models.conversation import Conversation
from ..models.message import Message, MessageRole
from ..repositories.conversation_repo import ConversationRepository
from .ai_client import AIAssistantRegistry
from shared.utils.logger import get_logger
from shared.utils.otel import get_current_trace_id

logger = get_logger("conversation-service")

class ConversationService:
    """对话业务服务 (v2.0: 通过 AIAssistantRegistry 支持多类型AI助手)"""
    
    def __init__(
        self, 
        repository: ConversationRepository,
        ai_registry: AIAssistantRegistry
    ):
        self.repository = repository
        self.ai_registry = ai_registry
        
    async def create_conversation(
        self,
        case_id: str,
        assistant_type: str = "openclaw",
        initial_message: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> Conversation:
        """创建新对话"""
        trace_id = get_current_trace_id()
        conversation = await self.repository.create_conversation(
            case_id=case_id,
            trace_id=trace_id,
            metadata=metadata
        )
        
        # 设置对话的助手类型
        conversation.assistant_type = assistant_type
        
        logger.info(
            event="conversation_created",
            message=f"Created conversation {conversation.conversation_id}",
            case_id=case_id,
            assistant_type=assistant_type,
            conversation_id=str(conversation.conversation_id)
        )
        
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
        assistant_type: str = "openclaw"
    ) -> AsyncGenerator[str, None]:
        """
        发送消息并获取流式回复 (v2.0: 根据 assistant_type 选择AI后端)
        
        1. 保存用户消息
        2. 获取历史上下文
        3. 从注册表获取对应 AI 客户端
        4. 流式返回响应
        """
        trace_id = get_current_trace_id()
        
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
        
        # 3. 从注册表获取AI助手客户端
        ai_client = self.ai_registry.get_client(assistant_type)
        if not ai_client:
            error_msg = f"未找到类型为 '{assistant_type}' 的AI助手"
            logger.error(event="ai_client_not_found", message=error_msg, assistant_type=assistant_type)
            yield f"\n[System Error: {error_msg}]"
            return
            
        # 4. 调用AI并流式返回
        try:
            async for chunk in ai_client.chat_completion_stream(
                messages=history_messages,
                user_id=f"case-{case_id}"
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
                    assistant_type=assistant_type,
                    error=str(e)
                )
                yield f"\n[System Error: {str(e)}]"
                raise

    async def save_assistant_message(
        self,
        conversation_id: uuid.UUID,
        case_id: str,
        content: str
    ) -> None:
        """保存AI返回的完整消息(后台执行)"""
        if not content:
            return
        
        trace_id = get_current_trace_id()
            
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
                response_length=len(content)
            )
        except Exception as e:
            logger.error(
                event="conversation_save_error",
                message="Error saving AI response in background",
                conversation_id=str(conversation_id),
                error=str(e)
            )
