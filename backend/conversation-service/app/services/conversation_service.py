"""
Conversation Service - 对话业务逻辑层 (v2.0 多类型AI助手)
"""

import uuid
from collections.abc import AsyncGenerator
from typing import Any

from shared.utils.logger import get_logger
from shared.utils.metrics import AI_REQUESTS_TOTAL, AI_TTFT_SECONDS
from shared.utils.otel import get_current_trace_id

from app.config import settings

from ..models.conversation import Conversation
from ..models.message import Message, MessageRole
from ..repositories.conversation_repo import ConversationRepository
from .ai_client import AIAssistantRegistry
from .kb_client import KBClient
from .scheduler_client import SchedulerClient

logger = get_logger("conversation-service")

_SYSTEM_BASE = """你是 HCI 智能排障助手，专门解答 HCI（超融合基础架构）产品的故障排查问题。

## 严格答复规则（不可违反）

1. **只能根据下方"参考资料"部分的内容回答问题**，不得使用训练数据中的其他知识。
2. **如果参考资料中没有相关内容**，必须直接回复：「当前知识库暂未收录该问题的解决方案，建议联系技术支持工程师。」
3. 严禁自由发挥、推测或补充参考资料未包含的内容。
4. 引用具体命令或步骤时，必须原文引用参考资料，不得修改或衍生。

## 答复格式要求

- 先定位根因，再给出可执行步骤
- 涉及命令时明确标注风险与前置条件
- 若参考资料信息不足以判断，先提最小必要澄清问题，不要猜测答案
"""


class ConversationService:
    """对话业务服务 (v2.0: 通过 AIAssistantRegistry 支持多类型AI助手)"""

    def __init__(
        self,
        repository: ConversationRepository,
        ai_registry: AIAssistantRegistry,
        scheduler_client: SchedulerClient | None = None,
        kb_client: KBClient | None = None,
    ):
        self.repository = repository
        self.ai_registry = ai_registry
        self.scheduler_client = scheduler_client
        self.kb_client = kb_client

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

    async def _build_system_prompt(self, query: str, case_id: str) -> str:
        """
        构建 4-Tier System Prompt：
          Tier1: 系统身份（固定）
          Tier2: SOP 精确命中（若有）
          Tier3: KB 语义检索 Chunks
          Tier4: 当前工单上下文
        """
        if not self.kb_client or not settings.KB_ENABLED:
            # KB 未启用时退回到基础 Prompt
            return _SYSTEM_BASE + f"\n\n---\n当前工单 ID：{case_id}"

        import asyncio

        # 并发执行 SOP 匹配 + 语义检索，降低延迟
        sop_task = asyncio.create_task(self.kb_client.sop_match(query))
        search_task = asyncio.create_task(self.kb_client.search(query, top_n=settings.KB_SEARCH_TOP_N))
        sop_node, kb_chunks = await asyncio.gather(sop_task, search_task, return_exceptions=True)

        # 容错：任一任务异常均不影响主流程
        if isinstance(sop_node, Exception):
            logger.warning(event="kb_sop_error", message=str(sop_node), case_id=case_id)
            sop_node = None
        if isinstance(kb_chunks, Exception):
            logger.warning(event="kb_search_error", message=str(kb_chunks), case_id=case_id)
            kb_chunks = []

        sections: list[str] = [_SYSTEM_BASE]
        has_context = False

        # --- Tier 2: SOP 精确命中 ---
        if sop_node:
            sop_content = sop_node.get("content", "")
            sop_title = sop_node.get("title") or sop_node.get("node_name") or "SOP"
            sections.append(
                f"## 📋 参考资料（精确匹配 SOP）：{sop_title}\n\n"
                f"{sop_content[:settings.KB_CONTEXT_MAX_CHARS]}"
            )
            has_context = True
            logger.info(
                event="kb_sop_hit",
                message=f"SOP 命中：{sop_title}",
                case_id=case_id,
                sop_title=sop_title,
            )

        # --- Tier 3: KB 语义检索 Chunks ---
        if kb_chunks:
            chunks_text_parts: list[str] = []
            total_chars = 0
            for i, chunk in enumerate(kb_chunks, 1):
                chunk_content = chunk.get("content", "")
                source = chunk.get("source_title") or chunk.get("document_title") or "未知文档"
                if total_chars + len(chunk_content) > settings.KB_CONTEXT_MAX_CHARS:
                    break
                chunks_text_parts.append(f"[{i}] 来源：{source}\n{chunk_content}")
                total_chars += len(chunk_content)

            if chunks_text_parts:
                sections.append(
                    "## 📚 参考资料（知识库语义检索）\n\n"
                    + "\n\n---\n\n".join(chunks_text_parts)
                )
                has_context = True
            logger.info(
                event="kb_search_hit",
                message=f"KB 检索命中 {len(kb_chunks)} 条",
                case_id=case_id,
                hit_count=len(kb_chunks),
            )

        # 无任何参考资料时，明确告知 LLM 不得擅自回答
        if not has_context:
            sections.append(
                "## ⚠️ 注意\n\n"
                "知识库中未检索到与本问题相关的参考资料。"
                "请按照严格答复规则第2条处理，直接告知用户知识库暂无相关内容，不得自行推测。"
            )
            logger.info(
                event="kb_no_hit",
                message="KB 无命中，将返回兜底提示",
                case_id=case_id,
            )

        # --- Tier 4: 工单上下文 ---
        sections.append(f"---\n当前工单 ID：{case_id}")

        return "\n\n".join(sections)

    async def send_message_stream_only(
        self, conversation_id: uuid.UUID, case_id: str, content: str, assistant_type: str | None = None
    ) -> AsyncGenerator[str, None]:
        """
        发送消息并获取流式回复 (v2.1: 4-Tier Prompt + KB 上下文注入)

        1. 保存用户消息
        2. 并发构建 4-Tier System Prompt（SOP + KB 检索）
        3. 获取历史上下文
        4. 从注册表获取对应 AI 客户端
        5. 流式返回响应
        """
        trace_id = get_current_trace_id()

        # 1. 保存用户消息
        await self.repository.add_message(
            conversation_id=conversation_id, case_id=case_id, role=MessageRole.user, content=content, trace_id=trace_id
        )

        # 2. 构建 4-Tier System Prompt（并发 SOP + 向量检索）
        system_prompt = await self._build_system_prompt(content, case_id)

        # 3. 获取历史上下文 (最近20条)
        all_messages = await self.repository.get_messages(conversation_id)
        history_messages: list[dict] = [{"role": "system", "content": system_prompt}]
        selected_messages = all_messages[-20:] if len(all_messages) > 20 else all_messages

        for msg in selected_messages:
            history_messages.append({"role": msg.role.value, "content": msg.content})

        # 4. 从注册表获取AI助手客户端
        resolved_assistant_type = await self._resolve_assistant_type(conversation_id, assistant_type)
        ai_client = self.ai_registry.get_client(resolved_assistant_type)
        if not ai_client:
            error_msg = f"未找到类型为 '{resolved_assistant_type}' 的AI助手"
            logger.error(event="ai_client_not_found", message=error_msg, assistant_type=resolved_assistant_type)
            yield f"\n[System Error: {error_msg}]"
            return

        pod_endpoint = await self._resolve_pod_endpoint(case_id, resolved_assistant_type)

        # 5. 调用AI并流式返回，同时记录 TTFT (Time To First Token)
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
                        # 记录首 Token 延迟到 Prometheus histogram
                        AI_TTFT_SECONDS.labels(assistant_type=resolved_assistant_type).observe(_ttft_ms / 1000.0)
                        _ttft_logged = True
                    yield chunk

            AI_REQUESTS_TOTAL.labels(assistant_type=resolved_assistant_type, status="success").inc()

        except Exception as e:
            import asyncio

            AI_REQUESTS_TOTAL.labels(assistant_type=resolved_assistant_type, status="error").inc()
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
        """优先走 scheduler 实时分配，失败则回退到静态 base_url。
        自动从 conversation metadata 取 case_title/case_description 并传给 scheduler。
        """
        if not self.scheduler_client:
            return self._get_fallback_endpoint(assistant_type)

        # 从当前 case 的 conversation metadata 获取工单信息（如果已存储）
        case_title: str | None = None
        case_description: str | None = None
        conversations = await self.repository.get_conversations_by_case(case_id)
        if conversations:
            meta = getattr(conversations[0], "metadata_", None) or {}
            case_title = meta.get("case_title")
            case_description = meta.get("case_description")

        allocated = await self.scheduler_client.allocate_pod(
            case_id, assistant_type,
            case_title=case_title,
            case_description=case_description,
        )
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
