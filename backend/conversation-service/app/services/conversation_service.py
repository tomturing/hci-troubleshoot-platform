"""
Conversation Service - 对话业务逻辑层 (v2.0 多类型 AI 助手)
"""

import asyncio
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
from .conversation_manager import ConversationManager
from .kb_client import KBClient
from .knowledge_extractor import KnowledgeExtractor
from .scheduler_client import SchedulerClient
from .sse_queue import LogAuditService, QueueSSEEmitter

logger = get_logger("conversation-service")

# Jaccard 相似度阈值
JACCARD_THRESHOLD = 0.6
# 历史消息采样数量
HISTORY_LIMIT = 10


def jaccard_similarity(a: str, b: str) -> float:
    """
    计算两个字符串的 Jaccard 相似度（token 级）

    中英文分词使用简单的 split()，不需要 jieba

    Args:
        a: 字符串 a
        b: 字符串 b

    Returns:
        相似度分数 (0.0-1.0)，若任一字符串为空则返回 0.0
    """
    sa, sb = set(a.lower().split()), set(b.lower().split())
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)

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


# ─── 5段式 Prompt 常量定义（替代旧版 _SYSTEM_BASE 的硬性拒绝逻辑）────────────
# Segment 1: 专家身份定义
_SEGMENT_IDENTITY = """你是深信服超融合基础设施（HCI）智能排障专家助手。
你拥有完整的 HCI 平台工作原理知识：虚拟机生命周期、分布式存储、vxlan网络、
IPMI硬件管理、acli诊断工具集的完整用法。
你的目标是协助现场工程师快速定位和解决 HCI 平台故障。"""

# Segment 2: 诊断方法论（{stage_desc} 在运行时填充当前阶段描述）
_SEGMENT_METHODOLOGY = """【工作方法论】
当前诊断阶段：{stage_desc}

标准诊断流程：
S0 意图识别：从客户描述提取关键实体（虚拟机名/集群/时间点），同时查看告警日志和操作日志，确认客户真实问题
S1 故障定位：向客户提出 1-3 个精准确认问题，定位到最小故障分类
S2 假设生成：列出 2-3 个最可能的根因假设，按概率排序
S3 验证执行：逐一执行诊断命令，收集系统状态证据
S4 根因确认：根据证据确定根因
S5 方案输出：提供明确可执行的修复步骤
S6 验证闭环：确认问题已解决，记录知识"""

# Segment 3: 推理规范（说明如何使用各类知识，不硬编码领域结论）
# ⚠️ 此段只描述"如何推理"，不硬编码特定故障类型的领域知识
# LLM 的领域知识来自训练权重，此处仅说明使用规范
_SEGMENT_REASONING_MODE = """【知识使用规范】
根据当前可用的参考资料，你的工作模式如下：

当有「SOP排障流程」时：
  - 这是针对此类故障的权威操作手册，按其步骤顺序执行
  - 在决策节点（有判断条件的步骤），先执行命令获取证据再做判断
  - 严格区分「临时修复步骤」和「永久解决方案」

当有「历史案例参考」时：
  - 这是历史上发生过的相似故障记录，用于辅助假设形成
  - 重点关注「根因」字段：它揭示了深层原因，是生成假设的核心依据
  - 不要直接照搬解决方案，当前环境版本/配置可能不同，需要先确认

当两者均无时（机制推理模式）：
  - 基于你对 HCI 平台工作原理的训练知识进行推理
  - 所有推断结论必须明确标注【机制推理】
  - 增加一条追问：收集更多信息以便触发知识库匹配"""

# Segment 4-SOP: SOP轨道命中时注入（程序性知识——按步骤执行）
_SEGMENT_SOP_REFERENCE = """【SOP 排障流程 | 来源：{sop_source}】
{sop_content}

请严格按照上述排障流程执行，在每个判断节点收集证据后再做决策。"""

# Segment 4-Case: KB案例轨道命中时注入（陈述性知识——提取假设）
_SEGMENT_CASE_REFERENCE = """【历史案例参考 | {case_count} 条相似案例】
{case_content}

上述案例供参考，重点关注「根因」字段中揭示的深层原因，用于形成假设。
当前环境可能与案例版本不同，执行修复方案前请先确认版本适用性。"""

# Segment 4-Fallback: 双轨均未命中时（机制推理兜底，替代旧版"拒绝回答"逻辑）
_SEGMENT_NO_REFERENCE = """【机制推理模式】
当前知识库中暂未找到与此故障高度匹配的 SOP 或历史案例。
请基于 HCI 平台架构机制知识进行推理：
  - 所有推断必须标注【机制推理】以提示用户这不是经过验证的排障步骤
  - 在回复末尾追加：「如您能提供更具体的报错信息（如错误码、任务ID），我可以尝试匹配更精确的排障流程」"""

# Segment 5: 当前工单上下文
_SEGMENT_CONTEXT_TEMPLATE = "---\n当前工单 ID：{case_id}"

# 诊断阶段描述映射（Phase 2 状态机实现后此映射会被动态值替换）
_STAGE_DESC_MAP = {
    "S0": "S0 - 意图识别",
    "S1": "S1 - 故障定位",
    "S2": "S2 - 假设生成",
    "S3": "S3 - 验证执行",
    "S4": "S4 - 根因确认",
    "S5": "S5 - 方案输出",
    "S6": "S6 - 验证闭环",
}


class ConversationService:
    """对话业务服务 (v2.0: 通过 AIAssistantRegistry 支持多类型 AI 助手)"""

    def __init__(
        self,
        repository: ConversationRepository,
        ai_registry: AIAssistantRegistry,
        scheduler_client: SchedulerClient | None = None,
        kb_client: KBClient | None = None,
        session_factory=None,
        tool_router=None,       # ToolRouter | None（Phase 4 agent 模式）
        confirm_service=None,   # ConfirmService | None
        glm_client=None,        # GLMClient | None（ReactExecutor 专用）
    ):
        self.repository = repository
        self.ai_registry = ai_registry
        self.scheduler_client = scheduler_client
        self.kb_client = kb_client
        # 独立事务 session 工厂，用于用户消息先行提交（与 AI 调用解耦）
        self.session_factory = session_factory
        # Phase 4: agent 模式组件（可选，未配置时回退到普通流式模式）
        self.tool_router = tool_router
        self.confirm_service = confirm_service
        self.glm_client = glm_client
        # Phase 4: 知识反馈闭环（可选，未配置时跳过）
        self.knowledge_extractor: KnowledgeExtractor | None = None
        self._audit_service = LogAuditService()
        # 诊断状态机（Phase 2）
        self._conversation_manager = ConversationManager()

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

    async def _build_system_prompt(
        self, query: str, case_id: str, diagnostic_stage: str = "S0"
    ) -> tuple[str, dict]:
        """
        构建 5段式 System Prompt（双轨知识架构 + 三级 Fallback）：
          Segment 1: 专家身份定义（固定）
          Segment 2: 诊断方法论（含当前诊断阶段）
          Segment 3: 推理规范（如何使用各类知识）
          Segment 4: 动态参考资料（三级 Fallback）
            → 优先级1：SOP 命中（程序性知识，按步骤执行）
            → 优先级2：KB 案例命中（陈述性知识，提取假设）
            → 优先级3：双轨均未命中（机制推理兜底，标注【机制推理】）
          Segment 5: 当前工单上下文

        返回：
            [0] system_prompt 字符串
            [1] audit_meta 字典，包含：
                - has_sop: bool
                - kb_chunks_count: int
                - kb_top_score: float | None
                - fallback_level: str （"sop" / "kb_case" / "mechanism"）
        """
        # 获取当前诊断阶段描述
        stage_desc = _STAGE_DESC_MAP.get(diagnostic_stage, f"{diagnostic_stage} - 进行中")

        # Segment 1 + 2 + 3 固定段
        base_sections: list[str] = [
            _SEGMENT_IDENTITY,
            _SEGMENT_METHODOLOGY.format(stage_desc=stage_desc),
            _SEGMENT_REASONING_MODE,
        ]

        if not self.kb_client or not settings.KB_ENABLED:
            # KB 未启用时，直接进入机制推理模式
            base_sections.append(_SEGMENT_NO_REFERENCE)
            base_sections.append(_SEGMENT_CONTEXT_TEMPLATE.format(case_id=case_id))
            return "\n\n".join(base_sections), {
                "has_sop": False,
                "kb_chunks_count": 0,
                "kb_top_score": None,
                "fallback_level": "mechanism",
            }

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

        fallback_level: str

        # --- Segment 4: 三级 Fallback ---
        if sop_node:
            # 优先级1: SOP 命中 → 程序性执行路径
            sop_content = sop_node.get("content", "")
            sop_title = sop_node.get("title") or sop_node.get("node_name") or "SOP 排障手册"
            base_sections.append(
                _SEGMENT_SOP_REFERENCE.format(
                    sop_source=sop_title,
                    sop_content=sop_content[:settings.KB_CONTEXT_MAX_CHARS],
                )
            )
            fallback_level = "sop"
            logger.info(
                event="kb_sop_hit",
                message=f"SOP 命中：{sop_title}",
                case_id=case_id,
                sop_title=sop_title,
            )

        elif kb_chunks:
            # 优先级2: KB 案例命中 → 假设生成路径
            chunks_text_parts: list[str] = []
            total_chars = 0
            for i, chunk in enumerate(kb_chunks, 1):
                chunk_content = chunk.get("content", "")
                source = chunk.get("source_title") or chunk.get("document_title") or "未知文档"
                case_id_ref = chunk.get("case_id", "")
                version = chunk.get("applicable_version", "")
                meta = f"案例 #{case_id_ref}" if case_id_ref else f"来源：{source}"
                if version:
                    meta += f" | 版本: {version}"
                if total_chars + len(chunk_content) > settings.KB_CONTEXT_MAX_CHARS:
                    break
                chunks_text_parts.append(f"[{i}] {meta}\n{chunk_content}")
                total_chars += len(chunk_content)

            if chunks_text_parts:
                base_sections.append(
                    _SEGMENT_CASE_REFERENCE.format(
                        case_count=len(chunks_text_parts),
                        case_content="\n\n---\n".join(chunks_text_parts),
                    )
                )
            fallback_level = "kb_case"
            logger.info(
                event="kb_search_hit",
                message=f"KB 检索命中 {len(kb_chunks)} 条",
                case_id=case_id,
                hit_count=len(kb_chunks),
            )

        else:
            # 优先级3: 双轨均未命中 → 机制推理兜底（不再拒绝回答）
            base_sections.append(_SEGMENT_NO_REFERENCE)
            fallback_level = "mechanism"
            logger.info(
                event="kb_no_hit",
                message="KB 双轨均未命中，进入机制推理模式",
                case_id=case_id,
            )

        # --- Segment 5: 工单上下文 ---
        base_sections.append(_SEGMENT_CONTEXT_TEMPLATE.format(case_id=case_id))

        # 构建审计元数据
        audit_meta = {
            "has_sop": sop_node is not None and not isinstance(sop_node, Exception),
            "kb_chunks_count": len(kb_chunks) if isinstance(kb_chunks, list) else 0,
            "kb_top_score": (
                max((c.get("score", 0.0) for c in kb_chunks), default=None)
                if isinstance(kb_chunks, list) and kb_chunks else None
            ),
            "fallback_level": fallback_level,
        }

        return "\n\n".join(base_sections), audit_meta

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

        # 1. 保存用户消息（独立事务，确保 AI 报错不会导致用户消息回滚）
        user_message: Message | None = None
        if self.session_factory:
            async with self.session_factory() as independent_session:
                user_message = await ConversationRepository(independent_session).add_message(
                    conversation_id=conversation_id, case_id=case_id, role=MessageRole.user, content=content, trace_id=trace_id
                )
                await independent_session.commit()
        else:
            user_message = await self.repository.add_message(
                conversation_id=conversation_id, case_id=case_id, role=MessageRole.user, content=content, trace_id=trace_id
            )

        # 1.5 重复提问检测（使用后台任务，避免阻塞主流程）
        if user_message:
            if self.session_factory:
                asyncio.create_task(
                    self._check_repeat_question_with_independent_session(
                        conversation_id=conversation_id,
                        case_id=case_id,
                        content=content,
                        current_message_id=user_message.message_id,
                    )
                )
            else:
                await self._check_repeat_question(
                    conversation_id=conversation_id,
                    case_id=case_id,
                    content=content,
                    current_message_id=user_message.message_id,
                )

        # 2. 读取当前诊断阶段并构建 System Prompt（并发 SOP + 向量检索）
        current_stage = "S0"
        if self.session_factory:
            async with self.session_factory() as stage_session:
                _conv = await ConversationRepository(stage_session).get_conversation(conversation_id)
                if _conv and _conv.diagnostic_stage:
                    current_stage = _conv.diagnostic_stage
        else:
            _conv = await self.repository.get_conversation(conversation_id)
            if _conv and _conv.diagnostic_stage:
                current_stage = _conv.diagnostic_stage

        system_prompt, _audit_meta = await self._build_system_prompt(content, case_id, diagnostic_stage=current_stage)

        # 3. 获取历史上下文 (最近 20 条)
        # 注意：必须使用独立 session，避免请求作用域 session 在流式传输期间长期持有事务锁
        # 导致后续 INSERT（包括 save_assistant_message 背景任务）等待锁无法落库
        if self.session_factory:
            async with self.session_factory() as msg_session:
                all_messages = await ConversationRepository(msg_session).get_messages(conversation_id)
        else:
            all_messages = await self.repository.get_messages(conversation_id)
        history_messages: list[dict] = [{"role": "system", "content": system_prompt}]
        selected_messages = all_messages[-20:] if len(all_messages) > 20 else all_messages

        for msg in selected_messages:
            history_messages.append({"role": msg.role.value, "content": msg.content})

        # 4. 从注册表获取 AI 助手客户端
        resolved_assistant_type = await self._resolve_assistant_type(conversation_id, assistant_type)
        ai_client = self.ai_registry.get_client(resolved_assistant_type)
        if not ai_client:
            error_msg = f"未找到类型为 '{resolved_assistant_type}' 的 AI 助手"
            logger.error(event="ai_client_not_found", message=error_msg, assistant_type=resolved_assistant_type)
            yield f"\n[System Error: {error_msg}]"
            return

        # 4.x 写入 prompt_audit（fire-and-forget，10% 采样完整 payload）
        import random
        _do_sample = random.random() < 0.10
        _sample_payload = history_messages if _do_sample else None

        if self.session_factory:
            asyncio.create_task(
                self._write_prompt_audit(
                    conversation_id=conversation_id,
                    case_id=case_id,
                    assistant_type=resolved_assistant_type,
                    trace_id=trace_id,
                    message_count=len(all_messages),
                    audit_meta=_audit_meta,
                    sample_payload=_sample_payload,
                )
            )

        pod_endpoint = await self._resolve_pod_endpoint(case_id, resolved_assistant_type)

        # 5. 调用 AI 并流式返回，同时记录 TTFT (Time To First Token)
        import time

        _stream_start = time.monotonic()
        _ttft_logged = False
        _full_reply_buffer: list[str] = []  # 收集完整回复用于阶段检测
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
                    _full_reply_buffer.append(chunk)
                    yield chunk

            AI_REQUESTS_TOTAL.labels(assistant_type=resolved_assistant_type, status="success").inc()

            # 6. 流式完成后，检测诊断阶段转换并持久化（fire-and-forget）
            full_reply = "".join(_full_reply_buffer)
            if full_reply:
                new_stage = self._conversation_manager.detect_stage_transition(
                    current_stage=current_stage,
                    assistant_reply=full_reply,
                    user_message=content,
                )
                if new_stage:
                    asyncio.create_task(
                        self._update_diagnostic_stage(
                            conversation_id=conversation_id,
                            new_stage=new_stage,
                            old_stage=current_stage,
                        )
                    )
                    # S6 阶段：触发知识反馈闭环（fire-and-forget，不阻塞对话响应）
                    if new_stage == "S6" and self.knowledge_extractor is not None:
                        asyncio.create_task(
                            self._trigger_knowledge_extraction(
                                conversation_id=conversation_id,
                                case_id=case_id,
                                messages=full_reply,
                                all_messages=_full_reply_buffer,
                            )
                        )

        except Exception as e:
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
        """保存 AI 返回的完整消息 (后台执行)

        注意：此方法由 BackgroundTasks 在响应完成后调用，届时请求作用域的
        self.repository.session 已被关闭（get_session finally close），
        必须使用 session_factory 创建独立 session 并显式 commit。
        """
        if not content:
            return

        trace_id = get_current_trace_id()

        try:
            if self.session_factory:
                async with self.session_factory() as independent_session:
                    await ConversationRepository(independent_session).add_message(
                        conversation_id=conversation_id,
                        case_id=case_id,
                        role=MessageRole.assistant,
                        content=content,
                        trace_id=trace_id,
                    )
                    await independent_session.commit()
            else:
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
        自动从 conversation metadata 获取 case_title/case_description 并传给 scheduler。
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

    async def _check_repeat_question_with_independent_session(
        self,
        conversation_id: uuid.UUID,
        case_id: str,
        content: str,
        current_message_id: uuid.UUID,
    ) -> None:
        """使用独立 session 执行重复提问检测，避免影响主请求事务。"""
        if not self.session_factory:
            return

        async with self.session_factory() as independent_session:
            repo = ConversationRepository(independent_session)
            await self._check_repeat_question(
                conversation_id=conversation_id,
                case_id=case_id,
                content=content,
                current_message_id=current_message_id,
                repository=repo,
            )
            await independent_session.commit()

    async def _check_repeat_question(
        self,
        conversation_id: uuid.UUID,
        case_id: str,
        content: str,
        current_message_id: uuid.UUID,
        repository: ConversationRepository | None = None,
    ) -> None:
        """
        检测用户是否重复提问，使用 Jaccard 相似度算法

        检测逻辑：
        1. 获取当前 case 下最近 N 条用户消息
        2. 计算新消息与历史消息的 Jaccard 相似度（token 级）
        3. 若任意一条历史消息 Jaccard >= 0.6，判定为重复提问
        4. 重复时：UPDATE conversation SET repeat_question_count = repeat_question_count + 1

        注意：此方法在消息保存后调用，不阻塞主流程
        """
        try:
            # 1. 获取当前 case 下最近 N 条用户消息（排除当前对话）
            repo = repository or self.repository
            recent_messages = await repo.get_recent_user_messages(
                case_id=case_id,
                current_message_id=current_message_id,
                limit=HISTORY_LIMIT,
            )

            if not recent_messages:
                return

            # 2. 计算与每条历史消息的 Jaccard 相似度
            is_repeat = False
            for historical_msg in recent_messages:
                similarity = jaccard_similarity(content, historical_msg.content)
                if similarity >= JACCARD_THRESHOLD:
                    is_repeat = True
                    logger.info(
                        event="repeat_question_detected",
                        message="检测到重复提问",
                        conversation_id=str(conversation_id),
                        case_id=case_id,
                        similarity=similarity,
                        historical_message_id=str(historical_msg.message_id),
                    )
                    break

            # 3. 如果是重复提问，增加计数
            if is_repeat:
                await repo.increment_repeat_question_count(conversation_id)
                logger.info(
                    event="repeat_question_count_increased",
                    message="重复提问计数已增加",
                    conversation_id=str(conversation_id),
                    case_id=case_id,
                )

        except Exception as e:
            # 检测失败不影响主流程，仅记录日志
            logger.error(
                event="repeat_question_check_error",
                message="重复提问检测失败",
                conversation_id=str(conversation_id),
                case_id=case_id,
                error=str(e),
            )

    # ─── Phase 4 Agent 模式（ReAct 推理 + 工具调用）────────────────────────────

    @property
    def agent_mode_available(self) -> bool:
        """检查 agent 模式所需组件是否已完整初始化"""
        return (
            self.tool_router is not None
            and self.confirm_service is not None
            and self.glm_client is not None
        )

    async def send_message_react_stream(
        self,
        session_id: str,
        conversation_id: uuid.UUID,
        case_id: str,
        content: str,
    ) -> AsyncGenerator[dict, None]:
        """
        Agent 模式流式响应（ReAct 推理 + 工具调用 + 人工确认）。

        产出序列：
          {"type": "thinking", "step": N, "message": "..."}      # 推理步骤 SSE 事件
          {"type": "confirm_request", "tool_name": ..., ...}      # 高风险操作确认请求
          {"type": "tool_executing", "tool": ..., "args": {...}}  # 工具执行通知
          {"content": "文本片段"}                                  # AI 文字回复块

        注意：需要先调用方确保 agent_mode_available=True。
        """
        from ..core.react_executor import AgentState, ReactExecutor

        trace_id = get_current_trace_id()

        # 1. 保存用户消息（独立事务）
        if self.session_factory:
            async with self.session_factory() as s:
                await ConversationRepository(s).add_message(
                    conversation_id=conversation_id,
                    case_id=case_id,
                    role=MessageRole.user,
                    content=content,
                    trace_id=trace_id,
                )
                await s.commit()
        else:
            await self.repository.add_message(
                conversation_id=conversation_id,
                case_id=case_id,
                role=MessageRole.user,
                content=content,
                trace_id=trace_id,
            )

        # 2. 构建 System Prompt
        system_prompt, _audit_meta = await self._build_system_prompt(content, case_id)

        # 3. 获取历史消息（独立 session 避免长事务）
        if self.session_factory:
            async with self.session_factory() as s:
                all_messages = await ConversationRepository(s).get_messages(conversation_id)
        else:
            all_messages = await self.repository.get_messages(conversation_id)

        history = [
            {"role": msg.role.value, "content": msg.content}
            for msg in (all_messages[-20:] if len(all_messages) > 20 else all_messages)
        ]

        # 4. 创建 per-request asyncio.Queue 桥接 SSE 事件与文本流
        queue: asyncio.Queue[dict | None] = asyncio.Queue()
        sse_emitter = QueueSSEEmitter(queue)

        # 5. 构建 AgentState 和 ReactExecutor
        state = AgentState(session_id=session_id, messages=history)
        executor = ReactExecutor(
            glm_client=self.glm_client,
            tool_executor=self.tool_router,
            confirm_service=self.confirm_service,
            audit_service=self._audit_service,
            sse_emitter=sse_emitter,
        )

        # 6. 在后台任务中运行 ReactExecutor，将结果放入队列
        async def _run_agent() -> None:
            try:
                async for chunk in executor.run(state, system_prompt):
                    if chunk:
                        await queue.put({"content": chunk})
            except Exception as exc:
                logger.error(
                    event="react_executor_error",
                    message=str(exc),
                    session_id=session_id,
                )
                await queue.put({"_error": str(exc)})
            finally:
                # 放入哨兵，通知消费者流结束
                await queue.put(None)

        asyncio.create_task(_run_agent())

        # 7. 从队列消费并 yield 事件
        while True:
            item = await queue.get()
            if item is None:
                break
            if "_error" in item:
                raise RuntimeError(item["_error"])
            yield item

    async def _trigger_knowledge_extraction(
        self,
        conversation_id: uuid.UUID,
        case_id: str,
        messages: str,          # 本轮 full_reply（类型占位，实际读取历史消息）
        all_messages: list[str], # _full_reply_buffer（类型占位）
    ) -> None:
        """S6 阶段触发知识提炼（fire-and-forget，失败不影响主流程）"""
        if self.knowledge_extractor is None:
            return
        try:
            # 读取完整对话历史
            if self.session_factory:
                async with self.session_factory() as s:
                    conversation_messages = await ConversationRepository(s).get_messages(conversation_id)
            else:
                conversation_messages = await self.repository.get_messages(conversation_id)

            msgs_dicts = [
                {"role": m.role.value, "content": m.content}
                for m in conversation_messages[-20:]
            ]

            # 获取工单分类（来自 case）
            category_id = ""
            if self.session_factory:
                async with self.session_factory() as s:
                    convs = await ConversationRepository(s).get_conversations_by_case(case_id)
                    if convs:
                        meta = getattr(convs[0], "metadata_", None) or {}
                        category_id = meta.get("category_id", "") or meta.get("classification", "")

            await self.knowledge_extractor.extract_from_session(
                session_id=str(conversation_id),
                conversation_messages=msgs_dicts,
                tool_audit_logs=[],  # MVP：工具调用日志暂不传递
                category_id=category_id,
            )
        except Exception as exc:
            logger.warning(
                event="knowledge_extraction_trigger_error",
                message=str(exc),
                conversation_id=str(conversation_id),
            )

    async def _write_prompt_audit(
        self,
        conversation_id: uuid.UUID,
        case_id: str,
        assistant_type: str,
        trace_id: str,
        message_count: int,
        audit_meta: dict,
        sample_payload: list | None,
    ) -> None:
        """写入 prompt_audit 记录（后台任务，失败不影响主流程）"""
        try:
            async with self.session_factory() as session:
                await ConversationRepository(session).insert_prompt_audit(
                    conversation_id=conversation_id,
                    case_id=case_id,
                    assistant_type=assistant_type,
                    trace_id=trace_id,
                    message_count=message_count,
                    has_sop=audit_meta["has_sop"],
                    kb_chunks_count=audit_meta["kb_chunks_count"],
                    kb_top_score=audit_meta["kb_top_score"],
                    messages=sample_payload,
                )
                await session.commit()
            logger.info(
                event="prompt_audit_written",
                conversation_id=str(conversation_id),
                case_id=case_id,
                has_sop=audit_meta["has_sop"],
                kb_chunks_count=audit_meta["kb_chunks_count"],
                sampled=sample_payload is not None,
            )
        except Exception as e:
            # 审计失败不影响主流程，只记录 warning
            logger.warning(
                event="prompt_audit_write_error",
                message=str(e),
                conversation_id=str(conversation_id),
            )

    async def _update_diagnostic_stage(
        self,
        conversation_id: uuid.UUID,
        new_stage: str,
        old_stage: str,
    ) -> None:
        """持久化诊断阶段转换（fire-and-forget 后台任务）

        使用独立事务 session 确保与主请求解耦。
        """
        from sqlalchemy import update as sa_update

        from ..models.conversation import Conversation as ConversationModel

        try:
            if self.session_factory:
                async with self.session_factory() as session:
                    await session.execute(
                        sa_update(ConversationModel)
                        .where(ConversationModel.conversation_id == conversation_id)
                        .values(diagnostic_stage=new_stage)
                    )
                    await session.commit()
            else:
                conv = await self.repository.get_conversation(conversation_id)
                if conv:
                    conv.diagnostic_stage = new_stage
                    await self.repository.session.flush()

            label = self._conversation_manager.get_stage_label
            logger.info(
                event="diagnostic_stage_transition",
                message=f"诊断阶段推进：{label(old_stage)} → {label(new_stage)}",
                conversation_id=str(conversation_id),
                old_stage=old_stage,
                new_stage=new_stage,
            )
        except Exception as e:
            logger.warning(
                event="diagnostic_stage_update_error",
                message=f"诊断阶段持久化失败：{e}",
                conversation_id=str(conversation_id),
                new_stage=new_stage,
            )
