"""
Conversation Service - 对话业务逻辑层 (v2.0 多类型AI助手)
"""

import uuid
from collections.abc import AsyncGenerator
from typing import Any

from shared.utils.logger import get_logger
from shared.utils.otel import get_current_trace_id

from app.config import settings

from ..models.conversation import Conversation
from ..models.message import Message, MessageRole
from ..repositories.conversation_repo import ConversationRepository
from .ai_client import AIAssistantRegistry
from .scheduler_client import SchedulerClient

logger = get_logger("conversation-service")


class ConversationService:
    """对话业务服务 (v2.0: 通过 AIAssistantRegistry 支持多类型AI助手)"""

    def __init__(
        self,
        repository: ConversationRepository,
        ai_registry: AIAssistantRegistry,
        scheduler_client: SchedulerClient | None = None,
    ):
        self.repository = repository
        self.ai_registry = ai_registry
        self.scheduler_client = scheduler_client

    async def create_conversation(
        self,
        case_id: str,
        assistant_type: str = "openclaw",
        initial_message: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Conversation:
        """创建新对话"""
        trace_id = get_current_trace_id()
        conversation = await self.repository.create_conversation(
            case_id=case_id, trace_id=trace_id, assistant_type=assistant_type, metadata=metadata
        )

        logger.info(
            event="conversation_created",
            message=f"Created conversation {conversation.conversation_id}",
            case_id=case_id,
            assistant_type=assistant_type,
            conversation_id=str(conversation.conversation_id),
        )

        return conversation

    async def get_conversation(self, conversation_id: uuid.UUID) -> Conversation | None:
        """获取对话详情"""
        return await self.repository.get_conversation(conversation_id)

    async def get_messages(self, conversation_id: uuid.UUID) -> list[Message]:
        """获取对话历史"""
        return await self.repository.get_messages(conversation_id)

    async def send_message_stream_only(
        self, conversation_id: uuid.UUID, case_id: str, content: str, assistant_type: str | None = None
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
            conversation_id=conversation_id, case_id=case_id, role=MessageRole.user, content=content, trace_id=trace_id
        )

        # 2. 获取历史上下文 (最近20条)
        all_messages = await self.repository.get_messages(conversation_id)
        history_messages = []
        selected_messages = all_messages[-20:] if len(all_messages) > 20 else all_messages

        for msg in selected_messages:
            history_messages.append({"role": msg.role.value, "content": msg.content})

        # 3. 从注册表获取AI助手客户端
        resolved_assistant_type = await self._resolve_assistant_type(conversation_id, assistant_type)
        ai_client = self.ai_registry.get_client(resolved_assistant_type)
        if not ai_client:
            error_msg = f"未找到类型为 '{resolved_assistant_type}' 的AI助手"
            logger.error(event="ai_client_not_found", message=error_msg, assistant_type=resolved_assistant_type)
            yield f"\n[System Error: {error_msg}]"
            return

        pod_endpoint = await self._resolve_pod_endpoint(case_id, resolved_assistant_type)

        # 4. 调用AI并流式返回，同时记录 TTFT (Time To First Token)
        import time

        _stream_start = time.monotonic()
        _ttft_logged = False
        try:
            async for chunk in ai_client.chat_completion_stream(
                messages=history_messages,
                user_id=f"case-{case_id}",
                pod_endpoint=pod_endpoint,
            ):
                if chunk:
                    if not _ttft_logged:
                        _ttft_ms = int((time.monotonic() - _stream_start) * 1000)
                        logger.info(
                            event="ai_ttft",
                            message="First token received",
                            ttft_ms=_ttft_ms,
                            assistant_type=resolved_assistant_type,
                            case_id=case_id,
                            conversation_id=str(conversation_id),
                        )
                        _ttft_logged = True
                    yield chunk

        except Exception as e:
            import asyncio

            if isinstance(e, asyncio.CancelledError):
                logger.info(event="stream_cancelled", message="Stream was cancelled by client")
                return
            logger.error(
                event="conversation_error",
                message="Error during AI generation",
                conversation_id=str(conversation_id),
                assistant_type=resolved_assistant_type,
                error=str(e),
            )
            # 不在此处 yield 内联错误文本，由上层路由 event_generator() 捕获此异常
            # 下层将统一以 "event: error\ndata: ..." 形式回传 SSE 结构化错误事件
            raise

    async def save_assistant_message(self, conversation_id: uuid.UUID, case_id: str, content: str) -> None:
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
                trace_id=trace_id,
            )
            logger.info(
                event="conversation_reply",
                message="AI response saved in background",
                conversation_id=str(conversation_id),
                response_length=len(content),
            )
        except Exception as e:
            logger.error(
                event="conversation_save_error",
                message="Error saving AI response in background",
                conversation_id=str(conversation_id),
                error=str(e),
            )

    async def _resolve_assistant_type(
        self,
        conversation_id: uuid.UUID,
        assistant_type: str | None,
    ) -> str:
        """优先使用显式参数，否则回退到 conversation.assistant_type。"""
        if assistant_type:
            return assistant_type
        conversation = await self.repository.get_conversation(conversation_id)
        if conversation and getattr(conversation, "assistant_type", None):
            return conversation.assistant_type
        return "openclaw"

    def _get_fallback_endpoint(self, assistant_type: str) -> str | None:
        cfg = settings.assistant_registry.get(assistant_type, {})
        endpoint = cfg.get("base_url")
        if endpoint:
            return str(endpoint).rstrip("/")
        return settings.OPENCLAW_BASE_URL.rstrip("/")

    async def _resolve_pod_endpoint(self, case_id: str, assistant_type: str) -> str | None:
        """优先走 scheduler 实时分配，失败则回退到静态 base_url。"""
        if not self.scheduler_client:
            return self._get_fallback_endpoint(assistant_type)

        allocated = await self.scheduler_client.allocate_pod(case_id, assistant_type)
        if not allocated:
            logger.warning(
                event="scheduler_allocate_unavailable",
                message="Scheduler allocation failed, fallback to static endpoint",
                case_id=case_id,
                assistant_type=assistant_type,
            )
            return self._get_fallback_endpoint(assistant_type)

        endpoint = await self.scheduler_client.wait_for_endpoint(case_id)
        if endpoint:
            logger.info(
                event="scheduler_endpoint_resolved",
                message=f"Resolved pod endpoint for case {case_id}",
                case_id=case_id,
                assistant_type=assistant_type,
                endpoint=endpoint,
            )
            return endpoint.rstrip("/")

        logger.warning(
            event="scheduler_endpoint_timeout",
            message="Pod endpoint not ready in time, fallback to static endpoint",
            case_id=case_id,
            assistant_type=assistant_type,
        )
        return self._get_fallback_endpoint(assistant_type)
