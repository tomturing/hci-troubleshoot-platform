"""
Conversation Service - 对话业务逻辑层 (v2.0 多类型 AI 助手)
"""

import asyncio
import re
import uuid
from collections.abc import AsyncGenerator
from typing import Any

from opentelemetry import trace
from shared.models.audit import AuditLog
from shared.utils.logger import get_logger
from shared.utils.metrics import AI_REQUESTS_TOTAL, AI_TTFT_SECONDS
from shared.utils.otel import get_current_trace_id

from app.config import settings

# T1-3 ~ T1-6: 大脑可选架构组件
from ..adapters.brain_router import BrainRouter
from ..core.brain_port import BrainInteractiveRequest, BrainStageUpdate, BrainTextChunk, BrainUnavailableError
from ..models.conversation import Conversation
from ..models.message import Message, MessageRole
from ..repositories.conversation_repo import ConversationRepository
from .ai_client import AIAssistantRegistry
from .conversation_manager import ConversationManager
from .environment_client import EnvironmentClient
from .kb_client import KBClient
from .knowledge_retriever import KnowledgeRetriever
from .prompt_builder import PromptBuilder
from .scheduler_client import SchedulerClient
from .sse_queue import LogAuditService, QueueSSEEmitter

logger = get_logger("conversation-service")
tracer = trace.get_tracer(__name__)

# Jaccard 相似度阈值
JACCARD_THRESHOLD = 0.6
# 历史消息采样数量
HISTORY_LIMIT = 10


def _bigram_tokens(s: str) -> set[str]:
    """
    字符级 bigram 分词，同时支持中文和英文。

    对整个字符串做字符级 bigram（含空格和标点），不区分中英文，统一处理。
    对长度 ≤ 1 的字符串，退化为单字符集合（避免空集）。
    """
    s = s.lower().strip()
    if not s:
        return set()
    if len(s) == 1:
        return {s}
    return {s[i : i + 2] for i in range(len(s) - 1)}


def jaccard_similarity(a: str, b: str) -> float:
    """
    计算两个字符串的 Jaccard 相似度（bigram 字符级）

    使用 bigram 字符级分词，原生支持中文（无需分词库）。
    修复了原 split() 方案对中文永远返回 0 的问题。

    Args:
        a: 字符串 a
        b: 字符串 b

    Returns:
        相似度分数 (0.0-1.0)，若任一字符串为空则返回 0.0
    """
    sa, sb = _bigram_tokens(a), _bigram_tokens(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


# ─── S3 阶段 acli 命令自动提取（P2）────────────────────────────────────────────

# 匹配 Markdown 代码块或行内代码中的 acli 命令
_ACLI_CMD_PATTERN = re.compile(
    r"```(?:bash|shell|sh)?\s*(acli\s+[^\n`]+)\s*```|`(acli\s+[^`\n]+)`",
    re.MULTILINE | re.IGNORECASE,
)

# 只读 acli 动词——满足则自动执行（无需人工确认）
_ACLI_READONLY_VERBS = frozenset(
    {"get", "list", "show", "describe", "status", "info", "check", "query", "fetch"}
)


def _extract_acli_commands(text: str) -> list[str]:
    """从 LLM 回复中提取 acli 命令（去重，保持顺序）。"""
    seen: set[str] = set()
    result: list[str] = []
    for m in _ACLI_CMD_PATTERN.finditer(text):
        cmd = (m.group(1) or m.group(2) or "").strip()
        if cmd and cmd not in seen:
            seen.add(cmd)
            result.append(cmd)
    return result


def _acli_is_readonly(cmd: str) -> bool:
    """判断 acli 命令是否为只读操作（可自动执行）。

    约定：acli <resource> <verb> ... 或 acli <verb> ...
    仅检查 `acli` 之后前两个 token（兼容 `<verb>` 或 `<resource> <verb>` 两种形式）：
    只要这两个候选位置之一是只读动词即认为只读（保守：不确定时 → False）。
    """
    parts = [p.lower() for p in cmd.strip().split()]
    return any(p in _ACLI_READONLY_VERBS for p in parts[1:3])  # 跳过 `acli`，仅检查 verb 候选位置


class ConversationService:
    """对话业务服务 (v2.0: 通过 AIAssistantRegistry 支持多类型 AI 助手)"""

    def __init__(
        self,
        repository: ConversationRepository,
        ai_registry: AIAssistantRegistry,
        scheduler_client: SchedulerClient | None = None,
        kb_client: KBClient | None = None,
        environment_client: EnvironmentClient | None = None,
        session_factory=None,
        tool_router=None,       # ToolRouter | None（Phase 4 agent 模式）
        confirm_service=None,   # ConfirmService | None
        glm_client=None,        # GLMClient | None（ReactExecutor 专用）
        brain_router: BrainRouter | None = None,  # T1-6: 大脑路由器（可选，渐进接入）
    ):
        self.repository = repository
        self.ai_registry = ai_registry
        self.scheduler_client = scheduler_client
        self.kb_client = kb_client
        self.environment_client = environment_client
        # 知识检索器（从 _build_system_prompt 提取）
        self._knowledge_retriever = KnowledgeRetriever(kb_client)
        # Prompt 构建器（S0 专用）
        self._prompt_builder = PromptBuilder()
        # 分类缓存（按域分组，用于 S0 阶段）
        self._categories_cache: dict[str, list[dict]] | None = None
        self._categories_cache_time: float = 0.0
        # 独立事务 session 工厂，用于用户消息先行提交（与 AI 调用解耦）
        self.session_factory = session_factory
        # Phase 4: agent 模式组件（可选，未配置时回退到普通流式模式）
        self.tool_router = tool_router
        self.confirm_service = confirm_service
        self.glm_client = glm_client
        self._audit_service = LogAuditService()
        # 诊断状态机（Phase 2）
        self._conversation_manager = ConversationManager()
        # T1-6: 大脑路由器（可选，None 时保持原有直接调用 ai_registry 的路径）
        self._brain_router: BrainRouter | None = brain_router

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
        self,
        query: str,
        case_id: str,
        diagnostic_stage: str = "S0",
        context_info: dict | None = None,
        confirmed_category_code: str | None = None,
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

        S0 阶段特殊处理：
          - 禁止 KB/SOP 检索
          - 注入 198 个分类列表
          - 注入环境信息、告警日志、任务日志

        返回：
            [0] system_prompt 字符串
            [1] audit_meta 字典，包含：
                - has_sop: bool
                - kb_chunks_count: int
                - kb_top_score: float | None
                - fallback_level: str （"sop" / "kb_case" / "mechanism" / "s0_intent_recognition"）
        """
        # S0 阶段：使用专用的 Prompt 构建方法
        if diagnostic_stage == "S0":
            return await self._build_s0_system_prompt(query, case_id, context_info)

        # S1+ 阶段：委托给 KnowledgeRetriever 处理
        # N-2 修复：传入 S0 已确认的分类编码，S1+ 跳过重复的意图识别 LLM 调用
        return await self._knowledge_retriever.retrieve(
            query=query,
            case_id=case_id,
            diagnostic_stage=diagnostic_stage,
            confirmed_category_code=confirmed_category_code,
        )

    async def _build_s0_system_prompt(
        self, query: str, case_id: str, context_info: dict | None = None
    ) -> tuple[str, dict]:
        """
        构建 S0 意图识别阶段的 System Prompt

        S0 阶段的特殊之处：
        1. 不进行 KB/SOP 检索
        2. 注入 198 个分类列表
        3. 注入环境信息、告警日志、任务日志

        参数：
            query: 用户查询（用于日志记录）
            case_id: 工单 ID
            context_info: 环境信息字典（可选）

        返回：
            [0] system_prompt 字符串
            [1] audit_meta 字典
        """
        # 获取分类列表（带缓存，5 分钟有效期）
        import time

        cache_ttl = 300.0  # 5 分钟
        if (
            self._categories_cache is None
            or (time.time() - self._categories_cache_time) > cache_ttl
        ):
            if self.kb_client:
                self._categories_cache = await self.kb_client.get_categories_grouped()
                self._categories_cache_time = time.time()
                logger.info(
                    event="s0_categories_loaded",
                    message=f"已加载 {sum(len(c) for c in self._categories_cache.values())} 个分类",
                    domain_count=len(self._categories_cache),
                )
            else:
                self._categories_cache = {}

        # 构建 S0 Prompt
        system_prompt = self._prompt_builder.build_s0_prompt(
            context_info=context_info or {},
            categories_by_domain=self._categories_cache or {},
            case_context={"case_id": case_id, "description": query},
        )

        # 构建 audit_meta
        total_chars = len(system_prompt)
        audit_meta = {
            "has_sop": False,
            "kb_chunks_count": 0,
            "kb_top_score": None,
            "fallback_level": "s0_intent_recognition",
            "context_breakdown": [
                {"code": "S0", "name": "意图识别", "chars": total_chars, "token_est": total_chars // 4}
            ],
            "total_chars": total_chars,
            "total_token_est": total_chars // 4,
            "category_id": None,
        }

        return system_prompt, audit_meta

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
        _confirmed_category_code: str | None = None  # N-2：S0 确认的分类编码
        if self.session_factory:
            async with self.session_factory() as stage_session:
                _conv = await ConversationRepository(stage_session).get_conversation(conversation_id)
                if _conv and _conv.diagnostic_stage:
                    current_stage = _conv.diagnostic_stage
                if _conv and getattr(_conv, "category_id", None):
                    _confirmed_category_code = _conv.category_id  # session 关闭前捕获
        else:
            _conv = await self.repository.get_conversation(conversation_id)
            if _conv and _conv.diagnostic_stage:
                current_stage = _conv.diagnostic_stage
            if _conv and getattr(_conv, "category_id", None):
                _confirmed_category_code = _conv.category_id

        # 2.5 S0 候选预处理（T3-c）：在调用 AI 之前拦截用户的 ①②③ 选择
        # 若用户回复的是序号，直接写库 + 推进/兜底，不走 AI 本轮
        if current_stage == "S0":
            _selection = self._conversation_manager.parse_candidate_selection(content)
            if _selection is not None:
                # 用户输入了 ①②③ 序号
                _candidates = await self._extract_s0_candidates(conversation_id)
                if _candidates:
                    _chosen = self._conversation_manager.resolve_candidate_category(_selection, _candidates)
                    if _chosen:
                        # 用户确认有效分类 → 写库、强制推进 S1，继续调用 AI 开始 S1 分析
                        asyncio.create_task(
                            self._update_conversation_category(
                                conversation_id=conversation_id,
                                case_id=case_id,
                                category_info=_chosen,
                                trigger_confirm=True,
                            )
                        )
                        asyncio.create_task(
                            self._update_diagnostic_stage(
                                conversation_id=conversation_id,
                                new_stage="S1",
                                old_stage="S0",
                            )
                        )
                        yield (
                            f"好的，确认故障分类为【{_chosen['code']} {_chosen['name']}】。\n"
                            "开始故障定位分析，请稍候…\n\n"
                        )
                        # 发出阶段切换事件通知前端，并继续以 S1 身份调用 AI
                        yield "\x00event:stage_change:S1\x00"
                        current_stage = "S1"
                    else:
                        # 用户选 ③"以上都不是"
                        _s0_rounds = await self._get_s0_candidate_rounds(conversation_id)
                        if ConversationManager.should_trigger_s0_failure(_s0_rounds):
                            _failure_msg = await self.handle_s0_failure(conversation_id, case_id)
                            yield _failure_msg
                            return
                        asyncio.create_task(self._increment_s0_candidate_rounds(conversation_id))
                        # 轮次未满：继续调用 AI，让其基于"以上都不是"重新给出候选
                # 若提取不到历史候选（AI 尚未给出 ① ② 时直接回了序号），交 AI 处理

        # 2.6 【修复】获取环境上下文信息（Segment 4 数据）
        context_info: dict | None = None
        if current_stage == "S0" and self.environment_client:
            env_context = await self.environment_client.get_context_info(case_id)
            if env_context:
                context_info = {
                    "env_info": env_context.env_info,
                    "alert_logs": env_context.alert_logs,
                    "task_logs": env_context.task_logs,
                }
                logger.info(
                    event="s0_context_info_loaded",
                    message="S0 Prompt 已加载环境上下文",
                    case_id=case_id,
                    alert_count=len(env_context.alert_logs),
                    task_count=len(env_context.task_logs),
                )

        system_prompt, _audit_meta = await self._build_system_prompt(
            content,
            case_id,
            diagnostic_stage=current_stage,
            context_info=context_info,
            # N-2 修复：S1+ 阶段复用 S0 确认的分类，跳过重复意图识别
            confirmed_category_code=_confirmed_category_code,
        )

        # T7: 若本次检索命中了 SOP，异步写入 conversation.sop_document_id 并更新 hit_count
        sop_document_id_from_meta = _audit_meta.get("sop_document_id") if _audit_meta else None
        if sop_document_id_from_meta is not None:
            asyncio.create_task(
                self._update_sop_usage(
                    conversation_id=conversation_id,
                    case_id=case_id,
                    sop_document_id=sop_document_id_from_meta,
                )
            )

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

        # 4.x 写入 prompt_audit（fire-and-forget，100% 采样完整 payload 用于审计分析）
        _do_sample = True  # 100% 采样，便于 Grafana Dashboard 审计 Agent 收到的完整上下文
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

        # 5. 调用大脑并流式返回
        # T1-6: 若已注入 BrainRouter，走新大脑可选路径；否则保持原有 ai_registry 路径（向后兼容）
        import time

        _full_reply_buffer: list[str] = []
        # N-4 修复：记录是否走了 ops-agent 路径（跳过 htp 状态机后验检测）
        _used_ops_agent_path = False
        try:
            if self._brain_router is not None:
                # ── 新路径：通过 BrainRouter 路由（ops-agent 或 htp 大脑）────────
                session_id = str(conversation_id)
                _used_ops_agent_path = (resolved_assistant_type == "ops-agent")
                async for brain_event in self._brain_router.process(
                    assistant_type=resolved_assistant_type,
                    session_id=session_id,
                    messages=history_messages,
                    env_context=context_info,  # 传入已预取的实时环境数据（N-3 修复）
                    stream=True,
                    case_id=case_id,
                    user_id=f"case-{case_id}",
                ):
                    if isinstance(brain_event, BrainTextChunk) and brain_event.content:
                        _full_reply_buffer.append(brain_event.content)
                        yield brain_event.content
                    elif isinstance(brain_event, BrainStageUpdate):
                        # 阶段变化事件：以内联标记传递给前端（保持向后兼容的 SSE 格式）
                        yield f"\x00event:stage_change:{brain_event.stage}\x00"
                    elif isinstance(brain_event, BrainInteractiveRequest):
                        # T-E6: ops-agent 交互请求 → 推送 interactive_request SSE 事件给前端
                        import json as _json
                        _ir_payload = _json.dumps({
                            "requestId": brain_event.request_id,
                            "acpSessionId": brain_event.acp_session_id,
                            "kind": brain_event.kind,
                            "title": brain_event.title,
                            "prompt": brain_event.prompt,
                            "options": brain_event.options,
                            "customInput": brain_event.custom_input,
                            "metadata": brain_event.metadata,
                        }, ensure_ascii=False)
                        yield f"\x00event:interactive_request:{_ir_payload}\x00"
                        # 将弹框内容以 assistant 角色异步落库，供历史记录查看
                        # metadata 结构必须与前端 SSE 处理后的嵌套格式一致：
                        # { kind: 'interactive_request', event: {...} }
                        # 否则历史加载时 MessageBubble.vue 读取 metadata.event 会得到 undefined
                        _ir_content = self._format_interactive_request_content(brain_event)
                        asyncio.create_task(
                            self._save_message_bg(
                                conversation_id=conversation_id,
                                case_id=case_id,
                                role=MessageRole.assistant,
                                content=_ir_content,
                                metadata={
                                    "kind": "interactive_request",
                                    "event": {
                                        "requestId": brain_event.request_id,
                                        "acpSessionId": brain_event.acp_session_id,
                                        "kind": brain_event.kind,
                                        "title": brain_event.title,
                                        "prompt": brain_event.prompt,
                                        "options": brain_event.options,
                                        "customInput": brain_event.custom_input,
                                        "metadata": brain_event.metadata,
                                    },
                                },
                            )
                        )
            else:
                # ── 原有路径：直接调用 ai_registry（BrainRouter 未注入时兜底）───
                ai_client = self.ai_registry.get_client(resolved_assistant_type)
                if not ai_client:
                    error_msg = f"未找到类型为 '{resolved_assistant_type}' 的 AI 助手"
                    logger.error(event="ai_client_not_found", message=error_msg, assistant_type=resolved_assistant_type)
                    yield f"\n[System Error: {error_msg}]"
                    return

                pod_endpoint = await self._resolve_pod_endpoint(case_id, resolved_assistant_type)
                _stream_start = time.monotonic()
                _ttft_logged = False
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

            # 6a. P2 修复：S3 阶段自动提取并执行只读 acli 命令
            full_reply = "".join(_full_reply_buffer)
            if full_reply and current_stage == "S3" and not _used_ops_agent_path and self.tool_router:
                acli_cmds = _extract_acli_commands(full_reply)
                for _cmd in acli_cmds:
                    if _acli_is_readonly(_cmd):
                        try:
                            _exec_result = await self.tool_router.execute(
                                "acli_run", {"command": _cmd}
                            )
                            _auto_block = (
                                f"\n\n**[自动执行]** `{_cmd}`\n```\n{_exec_result}\n```\n"
                            )
                            _full_reply_buffer.append(_auto_block)
                            yield _auto_block
                            logger.info(
                                event="s3_acli_auto_executed",
                                message=f"S3 阶段自动执行 acli 命令：{_cmd}",
                                case_id=case_id,
                                conversation_id=str(conversation_id),
                                command=_cmd,
                            )
                        except Exception as _acli_err:
                            logger.warning(
                                event="s3_acli_auto_exec_failed",
                                message=f"S3 acli 命令执行失败：{_acli_err}",
                                command=_cmd,
                                conversation_id=str(conversation_id),
                            )
                    else:
                        logger.debug(
                            event="s3_acli_write_skipped",
                            message=f"S3 写操作 acli 命令需用户确认，跳过自动执行：{_cmd}",
                            command=_cmd,
                        )

            # 6. 流式完成后，检测诊断阶段转换并持久化（fire-and-forget）
            full_reply = "".join(_full_reply_buffer)
            if full_reply and not _used_ops_agent_path:
                # N-4 修复：ops-agent 路径由其自身状态机管理阶段，跳过 htp 后验正则检测
                # 使用增强的阶段转换检测方法，同时提取分类信息
                new_stage, category_info = self._conversation_manager.detect_stage_transition_with_category(
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
                    # S3→S4 转换（实为 AI 输出根因时）：提取关联 KBD，写 resolved_kbd_entry_id
                    if new_stage == "S4" and current_stage == "S3":
                        kbd_entry_id = self._conversation_manager.extract_resolved_kbd(full_reply)
                        asyncio.create_task(
                            self._update_resolved_kbd(
                                conversation_id=conversation_id,
                                case_id=case_id,
                                kbd_entry_id=kbd_entry_id,
                            )
                        )
                # S0 阶段 category 写入与阶段转换解耦：只要 AI 输出了分类信息就写入
                if current_stage == "S0" and category_info:
                    asyncio.create_task(
                        self._update_conversation_category(
                            conversation_id=conversation_id,
                            case_id=case_id,
                            category_info=category_info,
                        )
                    )
            elif full_reply and _used_ops_agent_path:
                logger.debug(
                    event="ops_agent_stage_detection_skipped",
                    message="ops-agent 路径跳过 htp 状态机后验检测",
                    conversation_id=str(conversation_id),
                    current_stage=current_stage,
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

    async def save_assistant_message_for_resume(
        self, conversation_id: uuid.UUID, content: str
    ) -> None:
        """resume-stream 场景下保存 AI 续写回复（自动从 DB 查 case_id）。

        由 BackgroundTasks 在 resume-stream 响应完成后调用。
        """
        if not content:
            return
        try:
            if self.session_factory:
                async with self.session_factory() as s:
                    conv = await ConversationRepository(s).get_conversation(conversation_id)
            else:
                conv = await self.repository.get_conversation(conversation_id)
        except Exception as e:
            logger.warning(
                event="resume_stream_case_id_lookup_failed",
                message="resume 场景 case_id 查询失败，跳过落库",
                conversation_id=str(conversation_id),
                error=str(e),
            )
            return
        if conv is None:
            logger.warning(
                event="resume_stream_conv_not_found",
                message="resume 场景 conversation 不存在，跳过落库，避免写入孤儿记录",
                conversation_id=str(conversation_id),
            )
            return
        await self.save_assistant_message(conversation_id, conv.case_id, content)

    @staticmethod
    def _format_interactive_request_content(event: "BrainInteractiveRequest") -> str:
        """将 BrainInteractiveRequest 格式化为可读 Markdown 文字，用于落库到 message.content。"""
        lines: list[str] = []
        meta = event.metadata or {}
        if event.kind == "sop_step":
            lines.append("**📋 SOP 操作步骤确认**")
            if meta.get("route"):
                lines.append(f"\n**当前路径**：{meta['route']}")
            if meta.get("operationGoal"):
                lines.append(f"\n**操作目标**：{meta['operationGoal']}")
            if meta.get("expectedResult"):
                lines.append(f"\n**预期结果**：{meta['expectedResult']}")
            if meta.get("executionGuidance"):
                lines.append(f"\n**操作指引**：{meta['executionGuidance']}")
            feedback = meta.get("feedbackRequest") or event.prompt
            if feedback:
                lines.append(f"\n**请反馈**：{feedback}")
        else:
            lines.append("**❓ 信息确认**")
            question = meta.get("question") or event.prompt
            if question:
                lines.append(f"\n{question}")
            if meta.get("context"):
                lines.append(f"\n**背景说明**：{meta['context']}")
        if event.options:
            opt_parts = [f"{o.get('optionId', i + 1)}. {o.get('name', '')}" for i, o in enumerate(event.options)]
            lines.append(f"\n\n**可选项**：{'  /  '.join(opt_parts)}")
        return "\n".join(lines)

    @staticmethod
    def _format_interactive_response_content(outcome: dict) -> str:
        """将用户的交互响应格式化为可读文字，用于落库到 message.content。"""
        outcome_type = outcome.get("outcome", "")
        if outcome_type == "selected":
            label = outcome.get("optionLabel") or outcome.get("optionId", "")
            return f"[操作选择] {label}"
        elif outcome_type == "free_text":
            text = outcome.get("text", "")
            return f"[补充输入] {text}"
        import json as _json
        return f"[交互响应] {_json.dumps(outcome, ensure_ascii=False)}"

    async def _save_message_bg(
        self,
        conversation_id: uuid.UUID,
        case_id: str,
        role: "MessageRole",
        content: str,
        metadata: dict | None = None,
    ) -> None:
        """在独立 session 中后台保存消息（供 asyncio.create_task 调用）。"""
        if not content:
            return
        trace_id = get_current_trace_id()
        try:
            if self.session_factory:
                async with self.session_factory() as independent_session:
                    await ConversationRepository(independent_session).add_message(
                        conversation_id=conversation_id,
                        case_id=case_id,
                        role=role,
                        content=content,
                        trace_id=trace_id,
                        metadata=metadata or {},
                    )
                    await independent_session.commit()
            else:
                await self.repository.add_message(
                    conversation_id=conversation_id,
                    case_id=case_id,
                    role=role,
                    content=content,
                    trace_id=trace_id,
                    metadata=metadata or {},
                )
        except Exception as e:
            logger.error(
                event="save_message_bg_error",
                message="后台保存消息失败",
                conversation_id=str(conversation_id),
                role=str(role),
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

        with tracer.start_as_current_span("conversation.react_stream") as span:
            span.set_attribute("session_id", session_id)
            span.set_attribute("case_id", case_id)
            span.set_attribute("trace_id", trace_id or "")

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

            # 2. 构建 System Prompt（D-3 修复：读取当前诊断阶段，避免永远使用 S0 Prompt）
            react_stage = "S0"
            if self.session_factory:
                async with self.session_factory() as _s:
                    _react_conv = await ConversationRepository(_s).get_conversation(conversation_id)
                    if _react_conv and _react_conv.diagnostic_stage:
                        react_stage = _react_conv.diagnostic_stage
            else:
                _react_conv = await self.repository.get_conversation(conversation_id)
                if _react_conv and _react_conv.diagnostic_stage:
                    react_stage = _react_conv.diagnostic_stage
            system_prompt, _audit_meta = await self._build_system_prompt(
                content, case_id, diagnostic_stage=react_stage
            )

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
                    _err = RuntimeError(item["_error"])
                    span.record_exception(_err)
                    span.set_status(trace.StatusCode.ERROR, item["_error"])
                    raise _err
                yield item

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
        """写入 audit_log 记录（后台任务，失败不影响主流程）

        v6.2 重构：prompt_audit 功能已合并到 audit_log 表，
        直接使用 AuditLog ORM 模型写入。
        """
        try:
            async with self.session_factory() as session:
                audit_log = AuditLog(
                    conversation_id=conversation_id,
                    audit_type="prompt",
                    payload={
                        "case_id": case_id,
                        "assistant_type": assistant_type,
                        "message_count": message_count,
                        "has_sop": audit_meta["has_sop"],
                        "kb_chunks_count": audit_meta["kb_chunks_count"],
                        "kb_top_score": audit_meta["kb_top_score"],
                        "messages": sample_payload,
                        "context_breakdown": audit_meta.get("context_breakdown"),
                        "total_chars": audit_meta.get("total_chars"),
                        "total_token_est": audit_meta.get("total_token_est"),
                    },
                    trace_id=trace_id,
                )
                session.add(audit_log)
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
        确保内存更新在 DB commit 成功后执行，避免状态不一致。
        """
        from sqlalchemy import update as sa_update

        from ..models.conversation import Conversation as ConversationModel

        db_committed = False
        try:
            if self.session_factory:
                async with self.session_factory() as session:
                    await session.execute(
                        sa_update(ConversationModel)
                        .where(ConversationModel.conversation_id == conversation_id)
                        .values(diagnostic_stage=new_stage)
                    )
                    await session.commit()
                db_committed = True
                # DB 提交成功后更新内存
                conv = await self.repository.get_conversation(conversation_id)
                if conv:
                    conv.diagnostic_stage = new_stage
            else:
                conv = await self.repository.get_conversation(conversation_id)
                if conv:
                    conv.diagnostic_stage = new_stage
                    await self.repository.session.flush()
                db_committed = True

            label = self._conversation_manager.get_stage_label
            logger.info(
                event="diagnostic_stage_transition",
                message=f"诊断阶段推进：{label(old_stage)} → {label(new_stage)}",
                conversation_id=str(conversation_id),
                old_stage=old_stage,
                new_stage=new_stage,
                db_committed=db_committed,
            )
        except Exception as e:
            # DB 提交失败，内存不更新，记录错误日志
            logger.error(
                event="diagnostic_stage_update_error",
                message=f"诊断阶段持久化失败：{e}",
                conversation_id=str(conversation_id),
                old_stage=old_stage,
                new_stage=new_stage,
                db_committed=False,
                error=str(e),
            )

    async def _update_conversation_category(
        self,
        conversation_id: uuid.UUID,
        case_id: str,
        category_info: dict[str, str],
        trigger_confirm: bool = False,
    ) -> None:
        """
        更新 Conversation.category_id 并增加分类命中计数（fire-and-forget 后台任务）

        在 S0 阶段确认分类后调用：
        1. 更新 Conversation.category_id / category_l1 / category_l2
        2. Case 级去重：同一 case 已有 conversation 写入相同 category_id，跳过 hit +1
        3. 调用 KB Client 增加分类命中计数（仅首次）
        4. 仅当 trigger_confirm=True（用户明确选择 ①②③ 时）才触发 SP-1 confirm

        Args:
            conversation_id: 会话 ID
            case_id: 工单 ID（用于 case 级去重检查）
            category_info: 分类信息 {"code": "虚拟机-003", "name": "虚拟机开机失败"}
            trigger_confirm: 是否触发 SP-1 工单状态确认（True=用户明确选择，False=AI 回复解析）
        """
        from sqlalchemy import select
        from sqlalchemy import update as sa_update

        from ..models.conversation import Conversation as ConversationModel

        try:
            code = category_info.get("code", "")
            name = category_info.get("name", "")

            # 从 code 提取一级分类（域），如 "虚拟机-003" -> "虚拟机"
            category_l1 = code.split("-")[0] if "-" in code else ""

            # case 级去重：检查同 case 其他 conversation 是否已写入相同 category_id
            already_hit = False
            if self.session_factory and code:
                async with self.session_factory() as dedup_session:
                    result = await dedup_session.execute(
                        select(ConversationModel.conversation_id)
                        .where(
                            ConversationModel.case_id == case_id,
                            ConversationModel.category_id == code,
                            ConversationModel.conversation_id != conversation_id,
                        )
                        .limit(1)
                    )
                    already_hit = result.scalar_one_or_none() is not None

            if already_hit:
                logger.info(
                    event="category_hit_dedup_skipped",
                    message=f"case {case_id} 已有其他 conversation 命中分类 {code}，跳过计数",
                    conversation_id=str(conversation_id),
                    case_id=case_id,
                    category_id=code,
                )

            # 更新 Conversation
            if self.session_factory:
                async with self.session_factory() as session:
                    await session.execute(
                        sa_update(ConversationModel)
                        .where(ConversationModel.conversation_id == conversation_id)
                        .values(
                            category_id=code,
                            category_l1=category_l1,
                            category_l2=name,
                        )
                    )
                    await session.commit()
            else:
                conv = await self.repository.get_conversation(conversation_id)
                if conv:
                    conv.category_id = code
                    conv.category_l1 = category_l1
                    conv.category_l2 = name
                    await self.repository.session.flush()

            logger.info(
                event="conversation_category_updated",
                message=f"会话分类已更新：{code} {name}",
                conversation_id=str(conversation_id),
                category_id=code,
                category_l1=category_l1,
                category_l2=name,
            )

            # 增加 KB 分类命中计数（仅 case 首次）
            if self.kb_client and code and not already_hit:
                hit_count = await self.kb_client.increment_category_hit(code)
                logger.info(
                    event="category_hit_count_updated",
                    message=f"分类命中计数已更新：{code} -> {hit_count}",
                    code=code,
                    hit_count=hit_count,
                )

            # SP-1 同步点（T1）：仅用户明确选择 ①②③ 后才触发 confirm，防止 AI 生成候选时误触发
            # 404 视为幂等成功（已 confirmed）；非 200/404 仅 warning 不中断主流程
            if trigger_confirm and case_id and settings.CASE_SERVICE_URL:
                import httpx  # noqa: PLC0415
                try:
                    async with httpx.AsyncClient(timeout=5.0) as client:
                        resp = await client.put(
                            f"{settings.CASE_SERVICE_URL}/api/cases/{case_id}/confirm"
                        )
                    if resp.status_code not in (200, 404):
                        logger.warning(
                            event="case_confirm_failed",
                            case_id=case_id,
                            status_code=resp.status_code,
                        )
                    else:
                        logger.info(
                            event="case_confirmed_by_s0",
                            message=f"工单 {case_id} 已由 S0 分类写库触发 confirm",
                            case_id=case_id,
                        )
                except Exception as confirm_exc:
                    logger.warning(
                        event="case_confirm_error",
                        case_id=case_id,
                        error=str(confirm_exc),
                    )

        except Exception as e:
            logger.warning(
                event="conversation_category_update_error",
                message=f"会话分类更新失败：{e}",
                conversation_id=str(conversation_id),
                category_info=category_info,
            )

    async def _update_sop_usage(
        self,
        conversation_id: uuid.UUID,
        case_id: str,
        sop_document_id: int,
    ) -> None:
        """
        写入 conversation.sop_document_id 并触发 SOP hit_count +1（fire-and-forget 后台任务）

        Case 级去重：同一 case 已有 conversation 写入相同 sop_document_id，跳过 hit +1。

        Args:
            conversation_id: 当前会话 ID
            case_id: 工单 ID（用于 case 级去重）
            sop_document_id: SOP 文档 ID（来自 knowledge_retriever audit_meta）
        """
        from sqlalchemy import select
        from sqlalchemy import update as sa_update

        from ..models.conversation import Conversation as ConversationModel

        try:
            # case 级去重：检查同 case 其他 conversation 是否已写入相同 sop_document_id
            already_hit = False
            if self.session_factory:
                async with self.session_factory() as dedup_session:
                    result = await dedup_session.execute(
                        select(ConversationModel.conversation_id)
                        .where(
                            ConversationModel.case_id == case_id,
                            ConversationModel.sop_document_id == sop_document_id,
                            ConversationModel.conversation_id != conversation_id,
                        )
                        .limit(1)
                    )
                    already_hit = result.scalar_one_or_none() is not None

            if already_hit:
                logger.info(
                    event="sop_hit_dedup_skipped",
                    message=f"case {case_id} 已有其他 conversation 命中 SOP {sop_document_id}，跳过计数",
                    conversation_id=str(conversation_id),
                    case_id=case_id,
                    sop_document_id=sop_document_id,
                )

            # 写入 conversation.sop_document_id（仅首次写入，不覆盖已有值）
            if self.session_factory:
                async with self.session_factory() as session:
                    await session.execute(
                        sa_update(ConversationModel)
                        .where(
                            ConversationModel.conversation_id == conversation_id,
                            ConversationModel.sop_document_id.is_(None),
                        )
                        .values(sop_document_id=sop_document_id)
                    )
                    await session.commit()

            logger.info(
                event="conversation_sop_document_id_updated",
                message=f"会话 SOP 文档 ID 已写入：{sop_document_id}",
                conversation_id=str(conversation_id),
                sop_document_id=sop_document_id,
            )

            # 触发 SOP hit_count +1（仅 case 首次）
            if self.kb_client and not already_hit:
                await self.kb_client.increment_sop_hit(sop_document_id)
                logger.info(
                    event="sop_hit_count_updated",
                    message=f"SOP 命中计数已更新：{sop_document_id}",
                    sop_document_id=sop_document_id,
                    case_id=case_id,
                )

        except Exception as e:
            logger.warning(
                event="sop_usage_update_error",
                message=f"SOP 使用记录更新失败：{e}",
                conversation_id=str(conversation_id),
                sop_document_id=sop_document_id,
            )

    async def _update_resolved_kbd(
        self,
        conversation_id: uuid.UUID,
        case_id: str,
        kbd_entry_id: int | None,
    ) -> None:
        """
        写入 conversation.resolved_kbd_entry_id 并触发 KBD hit_count +1（fire-and-forget）

        S4 根因确认后调用：
        - kbd_entry_id 非 None → 写入字段 + hit +1
        - kbd_entry_id 为 None → 新问题未收录，仅记录日志

        Args:
            conversation_id: 当前会话 ID
            case_id: 工单 ID
            kbd_entry_id: KBD 条目 ID（None 表示新问题）
        """
        from sqlalchemy import select
        from sqlalchemy import update as sa_update

        from ..models.conversation import Conversation as ConversationModel

        if kbd_entry_id is None:
            logger.info(
                event="resolved_kbd_null",
                message=f"case {case_id} 根因确认为新问题，resolved_kbd_entry_id 为 NULL",
                conversation_id=str(conversation_id),
                case_id=case_id,
            )
            return

        try:
            # case 级去重：检查同 case 其他 conversation 是否已写入相同 resolved_kbd_entry_id
            already_hit = False
            if self.session_factory:
                async with self.session_factory() as dedup_session:
                    result = await dedup_session.execute(
                        select(ConversationModel.conversation_id)
                        .where(
                            ConversationModel.case_id == case_id,
                            ConversationModel.resolved_kbd_entry_id == kbd_entry_id,
                            ConversationModel.conversation_id != conversation_id,
                        )
                        .limit(1)
                    )
                    already_hit = result.scalar_one_or_none() is not None

            if already_hit:
                logger.info(
                    event="kbd_hit_dedup_skipped",
                    message=f"case {case_id} 已有其他 conversation 命中 KBD {kbd_entry_id}，跳过计数",
                    conversation_id=str(conversation_id),
                    case_id=case_id,
                    kbd_entry_id=kbd_entry_id,
                )

            # 写入 conversation.resolved_kbd_entry_id
            if self.session_factory:
                async with self.session_factory() as session:
                    await session.execute(
                        sa_update(ConversationModel)
                        .where(ConversationModel.conversation_id == conversation_id)
                        .values(resolved_kbd_entry_id=kbd_entry_id)
                    )
                    await session.commit()

            logger.info(
                event="conversation_resolved_kbd_updated",
                message=f"会话 resolved_kbd_entry_id 已写入：{kbd_entry_id}",
                conversation_id=str(conversation_id),
                kbd_entry_id=kbd_entry_id,
            )

            # 触发 KBD hit_count +1（仅 case 首次）
            if self.kb_client and not already_hit:
                await self.kb_client.increment_kbd_hit(kbd_entry_id)
                logger.info(
                    event="kbd_hit_count_updated",
                    message=f"KBD 命中计数已更新：{kbd_entry_id}",
                    kbd_entry_id=kbd_entry_id,
                    case_id=case_id,
                )

        except Exception as e:
            logger.warning(
                event="resolved_kbd_update_error",
                message=f"resolved_kbd_entry_id 更新失败：{e}",
                conversation_id=str(conversation_id),
                kbd_entry_id=kbd_entry_id,
            )

    # ─── S0 候选辅助方法 (T3-c) ─────────────────────────────────────────────

    async def _extract_s0_candidates(
        self,
        conversation_id: uuid.UUID,
    ) -> list[dict[str, str]]:
        """
        从上一条 assistant 消息中提取 S0 给出的候选分类列表。

        扫描最近一条 AI 消息，用正则提取 ① ② 对应的 {code, name}。
        格式示例：
          ① 虚拟机-003 虚拟机开机失败
          ② 存储-005 存储卷挂载异常

        Returns:
            list[dict]，每项格式 {"code": "...", "name": "..."}；
            无匹配时返回 []
        """
        import re

        from sqlalchemy import select

        from ..models.message import Message as MessageModel

        _candidate_item_pattern = re.compile(
            r"[①②]\s*([\u4e00-\u9fa5A-Za-z]+-\d+)\s+([\u4e00-\u9fa5A-Za-z0-9\s]+?)(?:\n|$)"
        )
        try:
            # 取最近一条 assistant 消息
            if self.session_factory:
                async with self.session_factory() as session:
                    result = await session.execute(
                        select(MessageModel.content)
                        .where(
                            MessageModel.conversation_id == conversation_id,
                            MessageModel.role == "assistant",
                        )
                        .order_by(MessageModel.created_at.desc())
                        .limit(1)
                    )
                    last_ai_content = result.scalar_one_or_none()
            else:
                msgs = await self.repository.get_messages(conversation_id)
                ai_msgs = [m for m in msgs if m.role.value == "assistant"]
                last_ai_content = ai_msgs[-1].content if ai_msgs else None

            if not last_ai_content:
                return []

            candidates: list[dict[str, str]] = []
            for m in _candidate_item_pattern.finditer(last_ai_content):
                candidates.append({"code": m.group(1).strip(), "name": m.group(2).strip()})
            return candidates
        except Exception as e:
            logger.warning(
                event="extract_s0_candidates_error",
                message=f"提取 S0 候选分类失败：{e}",
                conversation_id=str(conversation_id),
            )
            return []

    async def _get_s0_candidate_rounds(self, conversation_id: uuid.UUID) -> int:
        """
        读取 S0 候选确认已进行的轮次数。

        从 conversation.metadata_["s0_candidate_rounds"] 读取，默认 0。
        """
        from sqlalchemy import select

        from ..models.conversation import Conversation as ConversationModel

        try:
            if self.session_factory:
                async with self.session_factory() as session:
                    result = await session.execute(
                        select(ConversationModel.metadata_).where(
                            ConversationModel.conversation_id == conversation_id
                        )
                    )
                    meta = result.scalar_one_or_none() or {}
            else:
                conv = await self.repository.get_conversation(conversation_id)
                meta = conv.metadata_ if conv else {}
            return int((meta or {}).get("s0_candidate_rounds", 0))
        except Exception:
            return 0

    async def _increment_s0_candidate_rounds(self, conversation_id: uuid.UUID) -> None:
        """
        将 conversation.metadata_["s0_candidate_rounds"] 原子 +1 写回。

        N-5 修复：使用 PostgreSQL jsonb_set 原子更新，避免 Python 层
        read-modify-write 在并发场景下产生的竞态计数丢失。
        """
        from sqlalchemy import text as sa_text

        try:
            if self.session_factory:
                async with self.session_factory() as session:
                    await session.execute(
                        sa_text("""
                            UPDATE conversation
                            SET "metadata" = jsonb_set(
                                COALESCE("metadata", '{}'),
                                '{s0_candidate_rounds}',
                                (COALESCE(
                                    (COALESCE("metadata", '{}')->>'s0_candidate_rounds')::int,
                                    0
                                ) + 1)::text::jsonb
                            )
                            WHERE conversation_id = :cid
                        """),
                        {"cid": conversation_id},
                    )
                    await session.commit()
            else:
                # 无 session_factory 时降级为原有方式（测试场景兜底）
                conv = await self.repository.get_conversation(conversation_id)
                if conv:
                    meta = dict(conv.metadata_ or {})
                    meta["s0_candidate_rounds"] = int(meta.get("s0_candidate_rounds", 0)) + 1
                    conv.metadata_ = meta
                    await self.repository.session.flush()
        except Exception as e:
            logger.warning(
                event="increment_s0_rounds_error",
                message=f"递增 s0_candidate_rounds 失败：{e}",
                conversation_id=str(conversation_id),
            )

    # ─── S0 失败兜底 (v2) ────────────────────────────────────────────────────

    async def handle_s0_failure(
        self,
        conversation_id: uuid.UUID,
        case_id: str,
    ) -> str:
        """
        S0 意图识别彻底失败后的兜底处理。

        触发条件（满足任一）：
          - 候选确认轮次超过 S0_MAX_CANDIDATE_ROUNDS（默认 2 轮）
          - 用户两轮均选择 ③"以上都不是"

        行为：
          1. conversation.diagnostic_stage → "S0_FAILED"（标记失败原因）
          2. case.status: created → in_progress（跳过 confirmed，直接移交人工）
             close_reason 写 "s0_classification_failed"
          3. 返回面向用户的提示文本（由调用方 yield 给前端）

        Returns:
            str: 推送给用户的提示消息
        """
        from sqlalchemy import update as sa_update

        from ..models.conversation import Conversation as ConversationModel

        # 1. 标记 conversation 失败状态
        try:
            if self.session_factory:
                async with self.session_factory() as session:
                    await session.execute(
                        sa_update(ConversationModel)
                        .where(ConversationModel.conversation_id == conversation_id)
                        .values(diagnostic_stage="S0_FAILED")
                    )
                    await session.commit()
            else:
                conv = await self.repository.get_conversation(conversation_id)
                if conv:
                    conv.diagnostic_stage = "S0_FAILED"
                    await self.repository.session.flush()
        except Exception as e:
            logger.warning(
                event="s0_failure_stage_update_error",
                message=f"S0 失败状态写入异常：{e}",
                conversation_id=str(conversation_id),
            )

        # 2. case.status → in_progress（直接跳，不经过 confirmed）
        if self.scheduler_client:
            try:
                await self.scheduler_client.escalate_case_to_human(
                    case_id=case_id,
                    close_reason="s0_classification_failed",
                )
                logger.info(
                    event="s0_failure_escalated",
                    message=f"S0 分类失败，工单 {case_id} 已移交人工",
                    case_id=case_id,
                    conversation_id=str(conversation_id),
                )
            except Exception as e:
                logger.error(
                    event="s0_failure_escalate_error",
                    message=f"S0 兜底移交人工失败：{e}",
                    case_id=case_id,
                )

        return (
            "抱歉，当前 AI 助手无法确认您描述的故障类型，"
            f"已为您转接人工工程师处理。\n"
            f"工单编号：{case_id}，工程师将尽快与您联系。"
        )

    # ─── S6 三选项处理方法 (v6.3) ────────────────────────────────────────────

    async def send_s6_resolution_options(
        self,
        conversation_id: uuid.UUID,
    ) -> dict:
        """
        S6 阶段完成后，向用户推送三选项并持久化等待快照。

        调用时机：AI 完成 S6 VM 验证工具调用后，在推送 SSE 事件前调用此方法。

        流程：
          1. 构造 pending_resolution JSONB 快照
          2. 验证互斥约束（pending_confirm 必须为 NULL）
          3. 写入 DB（conversation.pending_resolution）
          4. 返回 SSE 消息体（由调用方推送）

        Args:
            conversation_id: 当前会话 ID

        Returns:
            dict: SSE 事件 payload，包含 event="s6_resolution_options_sent" 和 options

        Raises:
            ValueError: pending_confirm 非 NULL（约束 3 violation）
        """
        from sqlalchemy import update as sa_update

        from ..models.conversation import Conversation as ConversationModel

        conv = await self.repository.get_conversation(conversation_id)
        if conv is None:
            raise ValueError(f"会话不存在：{conversation_id}")

        # 约束 3：pending_resolution 和 pending_confirm 不能同时非 NULL
        if conv.pending_confirm is not None:
            raise ValueError(
                f"约束违反：pending_confirm 非 NULL（{conv.pending_confirm!r}）时"
                "不能设置 pending_resolution，两种等待状态互斥"
            )

        pending = self._conversation_manager.build_pending_resolution()

        if self.session_factory:
            async with self.session_factory() as session:
                await session.execute(
                    sa_update(ConversationModel)
                    .where(ConversationModel.conversation_id == conversation_id)
                    .values(pending_resolution=pending)
                )
                await session.commit()
        else:
            conv.pending_resolution = pending
            await self.repository.session.flush()

        logger.info(
            event="s6_resolution_options_sent",
            message="S6 三选项已推送，等待用户选择",
            conversation_id=str(conversation_id),
            sent_at=pending["sent_at"],
        )

        return {
            "event": "s6_resolution_options_sent",
            "data": {
                "message": "问题是否已解决？请选择：\nA. 是，问题已解决\nB. 否，还有新报错\nC. 需要人工支持",
                "options": pending["options"],
                "sent_at": pending["sent_at"],
            },
        }

    async def handle_s6_resolution_choice(
        self,
        conversation_id: uuid.UUID,
        case_id: str,
        choice: str,
    ) -> dict:
        """
        处理用户在 S6 三选项中的选择（A/B/C）。

        实现 4 条服务层强制约束（见 01_系统架构.md §9.6）：
          - 约束 1：resolved 只能从 confirmed 转入，且必须有 pending_resolution 快照
          - 约束 4 (B选项)：先 batch archive 旧 diagnostic_item，再改 stage

        Args:
            conversation_id: 当前会话 ID
            case_id: 工单 ID
            choice: 用户选择 "A"/"B"/"C"

        Returns:
            dict: 执行结果摘要

        Raises:
            ValueError: choice 不合法，或业务状态不满足约束
        """
        from sqlalchemy import update as sa_update

        from ..models.conversation import Conversation as ConversationModel
        from ..models.diagnostic_item import STATUS_ARCHIVED, DiagnosticItem

        # 获取动作描述（纯函数，不含副作用）
        action = self._conversation_manager.handle_resolution_choice(choice)  # type: ignore[arg-type]

        conv = await self.repository.get_conversation(conversation_id)
        if conv is None:
            raise ValueError(f"会话不存在：{conversation_id}")

        # 约束 1：resolved 只能在有 pending_resolution 的情况下发起
        if action["case_status"] == "resolved" and conv.pending_resolution is None:
            raise ValueError(
                "约束违反：选 A 要求 pending_resolution 非 NULL，但当前为 NULL，"
                "请先调用 send_s6_resolution_options()"
            )

        results: dict = {"choice": choice, "action": action["action"]}

        if self.session_factory:
            async with self.session_factory() as session:
                # 约束 4（B选项）：先 archive 旧 diagnostic_item
                if action["archive_diagnostic_items"]:
                    from sqlalchemy import and_
                    result = await session.execute(
                        sa_update(DiagnosticItem)
                        .where(
                            and_(
                                DiagnosticItem.conversation_id == conversation_id,
                                DiagnosticItem.status != STATUS_ARCHIVED,
                            )
                        )
                        .values(status=STATUS_ARCHIVED)
                    )
                    results["archived_count"] = result.rowcount
                    logger.info(
                        event="diagnostic_items_archived",
                        message=f"B 选项回退：已归档 {result.rowcount} 条诊断结论",
                        conversation_id=str(conversation_id),
                        archived_count=result.rowcount,
                    )

                # 更新 conversation 字段
                conv_updates: dict = {}
                if action["clear_pending_resolution"]:
                    conv_updates["pending_resolution"] = None
                if action["new_stage"]:
                    conv_updates["diagnostic_stage"] = action["new_stage"]

                if conv_updates:
                    await session.execute(
                        sa_update(ConversationModel)
                        .where(ConversationModel.conversation_id == conversation_id)
                        .values(**conv_updates)
                    )

                await session.commit()

        logger.info(
            event="s6_resolution_choice_handled",
            message=f"S6 用户选 {choice}，执行 {action['action']}",
            conversation_id=str(conversation_id),
            case_id=case_id,
            choice=choice,
            action=action["action"],
            new_case_status=action["case_status"],
        )

        return results

    async def submit_interactive_response(
        self,
        conversation_id: uuid.UUID,
        request_id: str,
        acp_session_id: str,
        outcome: dict,
    ) -> bool:
        """T-E6: 将用户对交互卡片的响应回传给 ops-agent ACP 会话。

        由 POST /api/conversations/{id}/interactive-response 调用。
        仅在 BrainRouter 已注入且 OpsAgentBrainAdapter 可用时生效。

        Args:
            conversation_id: 对话 ID（用于日志追踪）。
            request_id:      ACP request_id（来自 BrainInteractiveRequest.request_id）。
            acp_session_id:  ops-agent ACP session_id（来自 BrainInteractiveRequest.acp_session_id）。
            outcome:         提交结果，格式 {"outcome": "selected", "optionId": "A"}
                             或 {"outcome": "free_text", "text": "..."}。

        Returns:
            True  = 提交成功；False = OpsAgentBrainAdapter 不可用（ops-agent 未启用）。
        """
        if self._brain_router is None:
            logger.warning(
                event="interactive_response_no_router",
                message="submit_interactive_response: BrainRouter 未注入，跳过",
                conversation_id=str(conversation_id),
                request_id=request_id,
            )
            return False

        ops_adapter = self._brain_router.get_ops_agent_adapter()
        if ops_adapter is None:
            logger.warning(
                event="interactive_response_no_adapter",
                message="submit_interactive_response: OpsAgentBrainAdapter 未启用，跳过",
                conversation_id=str(conversation_id),
                request_id=request_id,
            )
            return False

        try:
            success = await ops_adapter.submit_acp_response(
                acp_session_id=acp_session_id,
                request_id=request_id,
                outcome=outcome,
            )
        except BrainUnavailableError as exc:
            logger.warning(
                event="interactive_response_brain_error",
                message="ops-agent ACP 接口不可达，交互响应已丢弃/已降级",
                conversation_id=str(conversation_id),
                acp_session_id=acp_session_id,
                request_id=request_id,
                error=str(exc),
            )
            return False
        logger.info(
            event="interactive_response_submitted",
            message="ops-agent 交互响应已回传",
            conversation_id=str(conversation_id),
            acp_session_id=acp_session_id,
            request_id=request_id,
            success=success,
        )
        # 将用户的弹框选择/输入以 user 角色落库，供历史记录查看
        try:
            conv = await self.repository.get_conversation(conversation_id)
            if conv:
                await self.repository.add_message(
                    conversation_id=conversation_id,
                    case_id=conv.case_id,
                    role=MessageRole.user,
                    content=self._format_interactive_response_content(outcome),
                    trace_id=get_current_trace_id(),
                    metadata={
                        "kind": "interactive_response",
                        "requestId": request_id,
                        "acpSessionId": acp_session_id,
                        "outcome": outcome,
                    },
                )
        except Exception as _e:
            logger.error(
                event="interactive_response_save_error",
                message="保存用户交互响应到 message 表失败",
                conversation_id=str(conversation_id),
                error=str(_e),
            )
        return success

    async def resume_ops_agent_stream(
        self,
        conversation_id: uuid.UUID,
    ) -> AsyncGenerator[str, None]:
        """恢复消费 ops-agent 续写事件流（不提交新 prompt）。

        适用场景：
        - 用户提交 interactive_response 后，ops-agent 的续写事件需要传回前端
        - 前端 SSE 连接因页面刷新而断开后重新接收事件

        实现：
        1. 通过 OpsAgentBrainAdapter.resume_event_stream() 消费 outbox
        2. 将 BrainEvent 序列化为与 send_message_stream_only 相同的 SSE 格式
        3. active_prompt=False 时立即返回，不挂起

        Yields:
            str: SSE 格式的文本/事件片段，与 send_message_stream_only 格式一致
        """
        import json as _json

        if self._brain_router is None:
            return

        ops_adapter = self._brain_router.get_ops_agent_adapter()
        if ops_adapter is None:
            return

        # 预查 case_id，供后续落库使用（interactive_request 需要关联 case）
        _conv = await self.repository.get_conversation(conversation_id)
        _case_id = _conv.case_id if _conv else ""

        session_id = str(conversation_id)
        async for brain_event in ops_adapter.resume_event_stream(session_id):
            if isinstance(brain_event, BrainTextChunk) and brain_event.content:
                yield brain_event.content
            elif isinstance(brain_event, BrainInteractiveRequest):
                _ir_payload = _json.dumps({
                    "requestId": brain_event.request_id,
                    "acpSessionId": brain_event.acp_session_id,
                    "kind": brain_event.kind,
                    "title": brain_event.title,
                    "prompt": brain_event.prompt,
                    "options": brain_event.options,
                    "customInput": brain_event.custom_input,
                    "metadata": brain_event.metadata,
                }, ensure_ascii=False)
                yield f"\x00event:interactive_request:{_ir_payload}\x00"
                # 新的交互请求也需要落库
                asyncio.create_task(
                    self._save_message_bg(
                        conversation_id=conversation_id,
                        case_id=_case_id,
                        role=MessageRole.assistant,
                        content=self._format_interactive_request_content(brain_event),
                        metadata={
                            "kind": "interactive_request",
                            "event": {
                                "requestId": brain_event.request_id,
                                "acpSessionId": brain_event.acp_session_id,
                                "kind": brain_event.kind,
                                "title": brain_event.title,
                                "prompt": brain_event.prompt,
                                "options": brain_event.options,
                                "customInput": brain_event.custom_input,
                                "metadata": brain_event.metadata,
                            },
                        },
                    )
                )
            elif isinstance(brain_event, BrainStageUpdate):
                yield f"\x00event:stage_change:{brain_event.stage}\x00"

