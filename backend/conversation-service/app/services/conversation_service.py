"""
Conversation Service - 对话业务逻辑层 (v2.0 多类型 AI 助手)
"""

import asyncio
import re
import secrets
import uuid
from collections.abc import AsyncGenerator
from typing import Any

from opentelemetry import context as otel_context
from opentelemetry import trace
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags
from shared.clients import AIAssistantRegistry, KBClient, SchedulerClient
from shared.models.audit import AuditLog
from shared.models.conversation import Conversation
from shared.observability.logger import get_logger
from shared.observability.metrics import AI_REQUESTS_TOTAL, AI_TTFT_SECONDS
from shared.observability.otel import get_current_trace_id

from app.config import settings

from ..models.message import Message, MessageRole
from ..repositories.conversation_repo import ConversationRepository
from ..repositories.sop_execution_repository import SopExecutionRepository
from .agent_client import AgentClient
from .conversation_manager import S0_NONE_OPTION_ID, ConversationManager
from .environment_client import EnvironmentClient
from .sse_queue import LogAuditService

logger = get_logger("conversation-service")
tracer = trace.get_tracer(__name__)

# Jaccard 相似度阈值
JACCARD_THRESHOLD = 0.6
# 历史消息采样数量
HISTORY_LIMIT = 10


def _build_remote_trace_context(trace_id_hex: str):
    """根据会话 trace_id 构造合法的远端父上下文。

    OpenTelemetry Python API 不提供 ``trace.generate_span_id``；Span ID 必须由调用方
    生成一个非零 64-bit 值。这里仅恢复父上下文，不伪造可导出的 Span。
    """
    if len(trace_id_hex) != 32:
        raise ValueError("trace_id 必须是 32 位十六进制字符串")
    trace_id = int(trace_id_hex, 16)
    if trace_id == 0:
        raise ValueError("trace_id 不能为全零")
    span_id = 0
    while span_id == 0:
        span_id = secrets.randbits(64)
    span_context = SpanContext(
        trace_id=trace_id,
        span_id=span_id,
        is_remote=True,
        trace_flags=TraceFlags(TraceFlags.SAMPLED),
    )
    return trace.set_span_in_context(NonRecordingSpan(span_context))


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
        agent_client: AgentClient | None = None,  # T1-6: agent-service HTTP 客户端
    ):
        self.repository = repository
        self.ai_registry = ai_registry
        self.scheduler_client = scheduler_client
        self.kb_client = kb_client
        self.environment_client = environment_client
        # 独立事务 session 工厂，用于用户消息先行提交（与 AI 调用解耦）
        self.session_factory = session_factory
        self._audit_service = LogAuditService()
        # 诊断状态机（Phase 2）
        self._conversation_manager = ConversationManager()
        # T1-6: agent-service 客户端（可选，None 时保持原有直接调用 ai_registry 的路径）
        self._agent_client: AgentClient | None = agent_client

    async def create_conversation(
        self,
        case_id: str,
        assistant_type: str = "htp-agent",
        initial_message: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Conversation:
        """创建新对话"""
        # 查询 Case 的 trace_id，确保一案一链继承机制
        case_trace_id = None
        try:
            from sqlalchemy import text

            res = await self.repository.session.execute(
                text('SELECT trace_id FROM "case" WHERE case_id = :case_id'), {"case_id": case_id}
            )
            case_row = res.fetchone()
            if case_row:
                case_trace_id = case_row[0]
        except Exception as e:
            logger.warning(
                event="failed_to_lookup_case_trace_id",
                case_id=case_id,
                error=str(e),
            )

        trace_id = case_trace_id or get_current_trace_id()
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
        self,
        conversation_id: uuid.UUID,
        case_id: str,
        content: str,
        assistant_type: str | None = None,
        metadata: dict | None = None,  # 接收 metadata
    ) -> AsyncGenerator[str, None]:
        """
        发送消息并获取流式回复 (v2.1: 4-Tier Prompt + KB 上下文注入)

        1. 保存用户消息
        2. 并发构建 4-Tier System Prompt（SOP + KB 检索）
        3. 获取历史上下文
        4. 从注册表获取对应 AI 客户端
        5. 流式返回响应
        """
        # 0. 获取并激活对齐的 OTel Trace ID 追踪上下文 (一案一链第一性原理)
        target_trace_id = None
        try:
            if self.session_factory:
                async with self.session_factory() as lookup_session:
                    _conv = await ConversationRepository(lookup_session).get_conversation(conversation_id)
                    if _conv and _conv.trace_id:
                        target_trace_id = _conv.trace_id
            else:
                _conv = await self.repository.get_conversation(conversation_id)
                if _conv and _conv.trace_id:
                    target_trace_id = _conv.trace_id
        except Exception as e:
            logger.warning(
                event="failed_to_lookup_conversation_trace_id",
                conversation_id=str(conversation_id),
                error=str(e),
            )

        # 构造并激活 OTEL Context
        ctx = None
        if target_trace_id and len(target_trace_id) == 32:
            try:
                ctx = _build_remote_trace_context(target_trace_id)
            except Exception as e:
                logger.warning(event="failed_to_build_otel_span_context", target_trace_id=target_trace_id, error=str(e))

        token = None
        if ctx:
            token = otel_context.attach(ctx)

        try:
            trace_id = get_current_trace_id()

            # 1. 保存用户消息（独立事务，确保 AI 报错不会导致用户消息回滚）
            user_message: Message | None = None
            if self.session_factory:
                async with self.session_factory() as independent_session:
                    user_message = await ConversationRepository(independent_session).add_message(
                        conversation_id=conversation_id,
                        case_id=case_id,
                        role=MessageRole.user,
                        content=content,
                        trace_id=trace_id,
                        metadata=metadata or {},  # 保存 metadata
                    )
                    await independent_session.commit()
            else:
                user_message = await self.repository.add_message(
                    conversation_id=conversation_id,
                    case_id=case_id,
                    role=MessageRole.user,
                    content=content,
                    trace_id=trace_id,
                    metadata=metadata or {},  # 保存 metadata
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
            intercepted_confirmation: str | None = None  # 记录拦截 S0 分类时产生的确认文本
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
            # v2 优先使用稳定 category code；圆圈序号仅兼容历史消息。
            if current_stage == "S0":
                _selection_meta = metadata or {}
                _stable_option_id = str(
                    _selection_meta.get("selectedCategoryCode") or _selection_meta.get("selectedOptionId") or ""
                ).strip()
                _legacy_selection = self._conversation_manager.parse_candidate_selection(content)
                if _stable_option_id or _legacy_selection is not None:
                    _candidates = await self._extract_s0_candidates(conversation_id)
                    if _candidates:
                        _none_selected = bool(_selection_meta.get("isNoneOfAbove")) or (
                            _stable_option_id == S0_NONE_OPTION_ID
                        )
                        if _stable_option_id and not _none_selected:
                            _chosen = self._conversation_manager.resolve_candidate_option(
                                _stable_option_id,
                                _candidates,
                            )
                            if not _chosen:
                                logger.warning(
                                    event="s0_stable_option_invalid",
                                    message="S0 结构化选项不属于最近候选，拒绝推进",
                                    conversation_id=str(conversation_id),
                                    selected_option_id=_stable_option_id,
                                )
                                yield "所选故障分类已失效，请重新描述故障并重新选择。"
                                return
                        elif _legacy_selection is not None and not _none_selected:
                            _chosen = self._conversation_manager.resolve_candidate_category(
                                _legacy_selection,
                                _candidates,
                            )
                            _none_selected = _chosen is None
                        else:
                            _chosen = None

                        if _chosen:
                            # 权威分类校验与 category/stage 同事务提交完成后，才能启动 S1。
                            _canonical = await self._validate_s0_category(_chosen)
                            if not _canonical:
                                yield "故障分类目录暂不可用或所选分类已失效，请稍后重新选择。"
                                return
                            _committed = await self._commit_s0_confirmation(
                                conversation_id=conversation_id,
                                case_id=case_id,
                                category_info=_canonical,
                            )
                            if not _committed:
                                yield "故障分类确认保存失败，尚未进入故障定位，请重试。"
                                return
                            intercepted_confirmation = (
                                f"好的，确认故障分类为【{_canonical['code']} {_canonical['name']}】。\n"
                                "开始故障定位分析，请稍候…"
                            )
                            yield (intercepted_confirmation + "\n\n")
                            # 发出阶段切换事件通知前端，并继续以 S1 身份调用 AI
                            yield "\x00event:stage_change:S1\x00"
                            current_stage = "S1"
                            _confirmed_category_code = _canonical["code"]
                        elif _none_selected:
                            # 用户选择“以上都不是”
                            _s0_rounds = await self._get_s0_candidate_rounds(conversation_id)
                            if ConversationManager.should_trigger_s0_failure(_s0_rounds):
                                _failure_msg = await self.handle_s0_failure(conversation_id, case_id)
                                yield _failure_msg
                                return
                            asyncio.create_task(self._increment_s0_candidate_rounds(conversation_id))
                            # 轮次未满：继续调用 AI，让其基于"以上都不是"重新给出候选
                    elif _stable_option_id:
                        logger.warning(
                            event="s0_stable_option_without_candidates",
                            message="收到结构化 S0 选择，但找不到对应候选消息",
                            conversation_id=str(conversation_id),
                            selected_option_id=_stable_option_id,
                        )
                        yield "找不到对应的故障分类候选，请重新描述故障并重新选择。"
                        return
                    # 只有旧消息直接回复序号且没有历史候选时，继续交给 S0 模型兼容处理。

            # 2.6 【修复】获取环境上下文信息（Segment 4 数据）
            context_info: dict | None = None
            if current_stage in ("S0", "S1", "S2", "S3", "S4") and self.environment_client:
                if settings.USE_RAW_ENVIRONMENT_CONTEXT:
                    raw_envs = await self.environment_client.get_raw_environments(case_id)
                    if raw_envs:
                        context_info = {
                            "is_raw": True,
                            "env_info": raw_envs.get("cluster", {}),
                            "alert_logs": raw_envs.get("alert", {}).get("alerts", []),
                            "task_logs": raw_envs.get("task", {}).get("tasks", []),
                        }
                        logger.info(
                            event="env_context_raw_info_loaded",
                            message=f"诊断阶段 {current_stage} 原始环境上下文已加载",
                            case_id=case_id,
                            alert_count=len(context_info["alert_logs"]),
                            task_count=len(context_info["task_logs"]),
                        )
                else:
                    env_context = await self.environment_client.get_context_info(case_id)
                    if env_context:
                        context_info = {
                            "env_info": env_context.env_info,
                            "alert_logs": env_context.alert_logs,
                            "task_logs": env_context.task_logs,
                        }
                        logger.info(
                            event="env_context_info_loaded",
                            message=f"诊断阶段 {current_stage} 环境上下文已加载",
                            case_id=case_id,
                            alert_count=len(env_context.alert_logs),
                            task_count=len(env_context.task_logs),
                        )

            # 3. 获取历史上下文 (最近 20 条)
            # 注意：必须使用独立 session，避免请求作用域 session 在流式传输期间长期持有事务锁
            # 导致后续 INSERT（包括 save_assistant_message 背景任务）等待锁无法落库
            if self.session_factory:
                async with self.session_factory() as msg_session:
                    all_messages = await ConversationRepository(msg_session).get_messages(conversation_id)
            else:
                all_messages = await self.repository.get_messages(conversation_id)

            # 构建消息历史（不再加入 system_prompt，由 agent-service 自己构建）
            # ReAct 工具调用恢复：将 tool_call/tool_result 消息还原为 OpenAI function calling 格式，
            # 使大模型在中断（步数限制/超时）后能完整续接 ReAct 推理链，无需从头重推。
            # 滑动窗口压缩策略：
            #   - 最近 TOOL_TURN_FULL_WINDOW 步（10步）的工具调用完整保留
            #   - 更早的工具调用输出截断为 200 字符摘要，节省 token 预算
            TOOL_TURN_FULL_WINDOW = 10  # 完整保留的最近工具调用步数
            TOOL_RESULT_SUMMARY_LEN = 200  # 早期工具结果摘要最大字符数
            MAX_TEXT_MESSAGES = 20  # 文本消息（user/assistant）最多保留条数

            # 第一步：分离文本消息和工具调用消息
            text_messages = [m for m in all_messages if m.role.value in ("user", "assistant", "system", "command")]
            tool_messages = [m for m in all_messages if m.role.value in ("tool_call", "tool_result")]

            # 第二步：文本消息按原有逻辑取最近 MAX_TEXT_MESSAGES 条
            selected_text = (
                text_messages[-MAX_TEXT_MESSAGES:] if len(text_messages) > MAX_TEXT_MESSAGES else text_messages
            )

            # 第三步：工具消息重组为 OpenAI messages 格式
            # tool_call 消息的 content 字段存储的是 JSON 序列化的 assistant message（含 tool_calls 数组）
            # tool_result 消息的 content 字段存储工具输出文本，tool_call_id 字段存储对应的 tool_call_id
            # 统计工具调用步数用于滑动窗口判定
            tool_call_messages = [m for m in tool_messages if m.role.value == "tool_call"]
            total_tool_steps = len(tool_call_messages)
            cutoff_step = total_tool_steps - TOOL_TURN_FULL_WINDOW  # 超过此步骤的工具输出需要摘要化

            import json as _json

            tool_step_idx = 0  # 当前 tool_call 的步骤序号
            reconstructed_tool_messages: list[dict] = []
            for msg in tool_messages:
                if msg.role.value == "tool_call":
                    tool_step_idx += 1
                    # tool_call 消息的 content 是 JSON 序列化的 OpenAI assistant message
                    try:
                        assistant_msg = _json.loads(msg.content)
                        reconstructed_tool_messages.append(assistant_msg)
                    except Exception:
                        # 解析失败则跳过（保证健壮性）
                        logger.warning(
                            event="tool_call_msg_parse_error",
                            message="tool_call 消息反序列化失败，已跳过",
                            conversation_id=str(conversation_id),
                            message_id=str(msg.message_id),
                        )
                elif msg.role.value == "tool_result":
                    # tool_result 消息按滑动窗口策略决定是否压缩
                    result_content = msg.content
                    # 早期工具输出（步骤 <= cutoff_step）且内容过长时截断为摘要
                    if (
                        cutoff_step > 0
                        and tool_step_idx <= cutoff_step
                        and len(result_content) > TOOL_RESULT_SUMMARY_LEN
                    ):
                        result_content = result_content[:TOOL_RESULT_SUMMARY_LEN] + "…（已截断，详情见工具执行日志）"
                    reconstructed_tool_messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": msg.tool_call_id or "",
                            "content": result_content,
                        }
                    )

            # 第四步：将文本消息和工具消息按时间顺序合并
            # 通过 created_at 排序合并，确保正确的 ReAct 时序
            combined_messages = sorted(
                [(m, "text") for m in selected_text]
                + [(m, "tool") for m in tool_messages if m.role.value in ("tool_call", "tool_result")],
                key=lambda x: x[0].created_at,
            )
            history_messages: list[dict] = []
            tool_msg_iter = iter(reconstructed_tool_messages)
            for msg, msg_type in combined_messages:
                if msg_type == "text":
                    history_messages.append({"role": msg.role.value, "content": msg.content})
                else:
                    # 工具消息按迭代器顺序插入
                    try:
                        history_messages.append(next(tool_msg_iter))
                    except StopIteration:
                        break

            if intercepted_confirmation:
                history_messages.append({"role": "assistant", "content": intercepted_confirmation})

            # T-AGT-23: 检测 SOP 执行恢复状态
            sop_resume_context: dict | None = None
            if self.session_factory:
                async with self.session_factory() as sop_session:
                    sop_repo = SopExecutionRepository(sop_session)
                    sop_execution = await sop_repo.get_active_by_conversation(conversation_id)
                    if sop_execution:
                        # 存在活跃的 SOP 执行，构建恢复上下文
                        sop_resume_context = {
                            "sop_document_id": sop_execution.sop_document_id,
                            "current_node_id": sop_execution.current_node_id,
                            "completed_steps": sop_execution.completed_steps or [],
                            "context_variables": sop_execution.context_variables or {},
                            "execution_log": sop_execution.execution_log or [],
                            "status": sop_execution.status,
                        }
                        logger.info(
                            event="sop_execution_resume_detected",
                            message="检测到活跃的 SOP 执行，构建恢复上下文",
                            conversation_id=str(conversation_id),
                            sop_document_id=sop_execution.sop_document_id,
                            current_node_id=sop_execution.current_node_id,
                            completed_steps_count=len(sop_execution.completed_steps or []),
                        )

            # 4. 从注册表获取 AI 助手客户端
            resolved_assistant_type = await self._resolve_assistant_type(conversation_id, assistant_type)

            # 5. 调用大脑并流式返回
            # T1-6: 若已注入 AgentRouter，走新大脑可选路径；否则保持原有 ai_registry 路径（向后兼容）
            import time

            _full_reply_buffer: list[str] = []
            # N-4 修复：记录是否走了 ops-agent 路径（跳过 htp 状态机后验检测）
            _used_ops_agent_path = False
            _message_metadata: dict = {}
            try:
                if self._agent_client is not None:
                    # ── 新路径：委托 agent-service（HTTP SSE）────────────────────
                    import json as _json

                    session_id = str(conversation_id)
                    _used_ops_agent_path = resolved_assistant_type == "ops-agent"
                    _agent_had_error = False

                    # 提取自动执行偏好
                    exec_mode = "safe-only"
                    if metadata and "auto_execute_mode" in metadata:
                        exec_mode = metadata["auto_execute_mode"]
                    elif metadata and "execution_mode" in metadata:
                        exec_mode = metadata["execution_mode"]

                    async for agent_event in self._agent_client.stream(
                        assistant_type=resolved_assistant_type,
                        session_id=session_id,
                        case_id=case_id,
                        user_id=f"case-{case_id}",
                        messages=history_messages,
                        env_context=context_info,
                        stream=True,
                        diagnostic_stage=current_stage,
                        category_id=_confirmed_category_code,
                        execution_mode=exec_mode,  # 传递自动执行模式
                        sop_resume_context=sop_resume_context,  # T-AGT-23: SOP 执行恢复上下文
                    ):
                        event_type = agent_event.get("type")
                        if event_type == "text_chunk":
                            _chunk = agent_event.get("content", "")
                            if _chunk:
                                _full_reply_buffer.append(_chunk)
                                yield _chunk
                        elif event_type == "stage_update":
                            _stage = agent_event.get("stage", "")
                            _metadata = agent_event.get("metadata", {})
                            yield f"\x00event:stage_change:{_stage}\x00"
                            if _stage in ("tool_call", "tool_result"):
                                # 工具生命周期先按 exec_id 持久化，再向 SSE 发布。
                                # 这样即使客户端随即断线或 Pod 滚动，审计状态也不会丢失。
                                await self._record_tool_call(
                                    conversation_id=conversation_id,
                                    case_id=case_id,
                                    stage=_stage,
                                    metadata=_metadata,
                                )
                                _payload = _json.dumps(_metadata, ensure_ascii=False)
                                yield f"\x00event:{_stage}:{_payload}\x00"
                            # T-AGT-07: 处理 SOP 命中统计（sop_reasoning 事件携带 sop_document_id）
                            if _stage == "sop_reasoning" and _metadata.get("sop_document_id"):
                                _sop_doc_id = _metadata.get("sop_document_id")
                                if _sop_doc_id and isinstance(_sop_doc_id, int):
                                    asyncio.create_task(
                                        self._update_sop_usage(
                                            conversation_id=conversation_id,
                                            case_id=case_id,
                                            sop_document_id=_sop_doc_id,
                                        )
                                    )
                        elif event_type == "interactive_request":
                            # S0 意图识别的 interactive_request：将其转换为消息元数据并发送至前端渲染
                            if agent_event.get("kind") == "intent_selection":
                                logger.info(
                                    event="intent_selection_interactive_request_metadata",
                                    message="S0 意图识别转换为消息的 metadata，通过 SSE 发送给前端并传导至后台落库",
                                    conversation_id=str(conversation_id),
                                )
                                _intent_metadata = {
                                    "kind": "choice_options",
                                    "schemaVersion": 2,
                                    "requestId": agent_event.get("request_id"),
                                    "options": agent_event.get("options"),
                                }
                                _message_metadata.update(_intent_metadata)

                                _payload = _json.dumps(_intent_metadata, ensure_ascii=False)
                                yield f"\x00event:metadata:{_payload}\x00"
                                continue
                            _ir_payload = _json.dumps(
                                {
                                    "requestId": agent_event.get("request_id"),
                                    "acpSessionId": agent_event.get("acp_session_id"),
                                    "kind": agent_event.get("kind"),
                                    "title": agent_event.get("title"),
                                    "prompt": agent_event.get("prompt"),
                                    "options": agent_event.get("options"),
                                    "customInput": agent_event.get("custom_input"),
                                    "metadata": agent_event.get("metadata"),
                                    "execId": agent_event.get("exec_id"),
                                    "inputHash": agent_event.get("input_hash"),
                                    "expiresAt": agent_event.get("expires_at"),
                                },
                                ensure_ascii=False,
                            )
                            # Bug2 修复保持：先落库再 yield
                            _ir_content = self._format_interactive_request_content_dict(agent_event)
                            asyncio.create_task(
                                self._save_message_bg(
                                    conversation_id=conversation_id,
                                    case_id=case_id,
                                    role=MessageRole.assistant,
                                    content=_ir_content,
                                    metadata={
                                        "kind": "interactive_request",
                                        "event": {
                                            "requestId": agent_event.get("request_id"),
                                            "acpSessionId": agent_event.get("acp_session_id"),
                                            "kind": agent_event.get("kind"),
                                            "title": agent_event.get("title"),
                                            "prompt": agent_event.get("prompt"),
                                            "options": agent_event.get("options"),
                                            "customInput": agent_event.get("custom_input"),
                                            "metadata": agent_event.get("metadata"),
                                            "execId": agent_event.get("exec_id"),
                                            "inputHash": agent_event.get("input_hash"),
                                            "expiresAt": agent_event.get("expires_at"),
                                        },
                                    },
                                )
                            )
                            yield f"\x00event:interactive_request:{_ir_payload}\x00"
                        elif event_type == "escalation":
                            _escalation_event = self._escalation_interactive_event(
                                conversation_id,
                                agent_event,
                            )
                            _payload = _json.dumps(_escalation_event, ensure_ascii=False)
                            _message_metadata.update({"kind": "human_escalation", "event": _escalation_event})
                            yield f"\x00event:interactive_request:{_payload}\x00"
                        elif event_type == "error":
                            _agent_had_error = True
                            yield f"\n[Agent Error: {agent_event.get('message', '未知错误')}]"
                        elif event_type == "done":
                            break

                    # 空响应兜底：agent-service 返回 done 但没有任何内容（且未收到 error 事件）
                    if not _full_reply_buffer and not _agent_had_error:
                        logger.warning(
                            event="agent_empty_response",
                            message="agent-service 返回空响应（无 text_chunk）",
                            conversation_id=str(conversation_id),
                            assistant_type=resolved_assistant_type,
                        )
                        yield "\n[系统提示] AI 助手暂未返回内容，可能是服务暂时繁忙或配置异常。请稍后重试。\n"
                else:
                    # ── 原有路径：直接调用 ai_registry（AgentRouter 未注入时兜底）───
                    ai_client = self.ai_registry.get_client(resolved_assistant_type)
                    if not ai_client:
                        error_msg = f"未找到类型为 '{resolved_assistant_type}' 的 AI 助手"
                        logger.error(
                            event="ai_client_not_found", message=error_msg, assistant_type=resolved_assistant_type
                        )
                        yield f"\n[System Error: {error_msg}]"
                        return

                    pod_endpoint = await self._resolve_pod_endpoint(case_id, resolved_assistant_type)
                    _stream_start = time.monotonic()
                    _ttft_logged = False
                    async for chunk in ai_client.chat_completion_stream(
                        messages=history_messages,
                        user_id=f"case-{case_id}",
                        pod_endpoint=pod_endpoint,
                        temperature=settings.LLM_TEMPERATURE_S0,
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
                                AI_TTFT_SECONDS.labels(assistant_type=resolved_assistant_type).observe(
                                    _ttft_ms / 1000.0
                                )
                                _ttft_logged = True
                            _full_reply_buffer.append(chunk)
                            yield chunk

                AI_REQUESTS_TOTAL.labels(assistant_type=resolved_assistant_type, status="success").inc()

                # 审计日志写入已完全下沉至 agent-service：在发起 LLM 调用的瞬间捕获 100% 原始全量 Prompt 并安全记录。
                # 此处废除重复双写。

                # 流式完成后，检测诊断阶段转换并持久化（fire-and-forget）
                full_reply = "".join(_full_reply_buffer)
                if full_reply and not _used_ops_agent_path and current_stage != "S0":
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
                elif full_reply and current_stage == "S0":
                    # S0 模型只能提出候选。分类和 S1 阶段只能由用户的结构化确认
                    # 触发 _commit_s0_confirmation，禁止根据模型原文后验自动写库。
                    logger.debug(
                        event="s0_model_stage_detection_skipped",
                        message="S0 候选已返回，等待用户结构化确认",
                        conversation_id=str(conversation_id),
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

        finally:
            if token is not None:
                otel_context.detach(token)

    async def save_assistant_message(
        self,
        conversation_id: uuid.UUID,
        case_id: str,
        content: str,
        metadata: dict | None = None,
    ) -> None:
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
                        metadata=metadata,
                    )
                    await independent_session.commit()
            else:
                await self.repository.add_message(
                    conversation_id=conversation_id,
                    case_id=case_id,
                    role=MessageRole.assistant,
                    content=content,
                    trace_id=trace_id,
                    metadata=metadata,
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
        self,
        conversation_id: uuid.UUID,
        content: str,
        metadata: dict | None = None,
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
        await self.save_assistant_message(conversation_id, conv.case_id, content, metadata)

    @staticmethod
    def _format_interactive_request_content(event: Any) -> str:
        """将 AgentInteractiveRequest 格式化为可读 Markdown 文字，用于落库到 message.content。"""
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
    def _format_interactive_request_content_dict(event: dict) -> str:
        """将 agent-service 返回的 interactive_request dict 格式化为可读 Markdown 文字。"""
        _lines: list[str] = []
        meta = event.get("metadata") or {}
        kind = event.get("kind", "")
        prompt = event.get("prompt", "")
        options = event.get("options") or []
        if kind == "sop_step":
            _lines.append("**📋 SOP 操作步骤确认**")
            if meta.get("route"):
                _lines.append(f"\n**当前路径**：{meta['route']}")
            if meta.get("operationGoal"):
                _lines.append(f"\n**操作目标**：{meta['operationGoal']}")
            if meta.get("expectedResult"):
                _lines.append(f"\n**预期结果**：{meta['expectedResult']}")
            if meta.get("executionGuidance"):
                _lines.append(f"\n**操作指引**：{meta['executionGuidance']}")
            feedback = meta.get("feedbackRequest") or prompt
            if feedback:
                _lines.append(f"\n**请反馈**：{feedback}")
        else:
            _lines.append("**❓ 信息确认**")
            question = meta.get("question") or prompt
            if question:
                _lines.append(f"\n{question}")
            if meta.get("context"):
                _lines.append(f"\n**背景说明**：{meta['context']}")
        if options:
            opt_parts = [f"{o.get('optionId', i + 1)}. {o.get('name', '')}" for i, o in enumerate(options)]
            _lines.append(f"\n\n**可选项**：{'  /  '.join(opt_parts)}")
        return "\n".join(_lines)

    @staticmethod
    def _escalation_interactive_event(conversation_id: uuid.UUID | str, event: dict) -> dict:
        """把 AgentEscalation 转成前端可见的 human_escalation 交互契约。"""
        return {
            "requestId": f"escalation-{conversation_id}",
            "acpSessionId": str(conversation_id),
            "kind": "human_escalation",
            "title": "需要人工支持",
            "prompt": event.get("reason") or "自动诊断证据不足，已转人工处理。",
            "options": [{"optionId": "ack", "name": "我知道了"}],
            "customInput": False,
            "metadata": event.get("context") or {},
        }

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
        tool_call_id: str | None = None,
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
                        tool_call_id=tool_call_id,
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
                    tool_call_id=tool_call_id,
                )
        except Exception as e:
            logger.error(
                event="save_message_bg_error",
                message="后台保存消息失败",
                conversation_id=str(conversation_id),
                role=str(role),
                error=str(e),
            )

    async def save_tool_turn(
        self,
        conversation_id: uuid.UUID,
        case_id: str,
        tool_calls_msg: dict,
        tool_result_msg: dict,
        exec_id: str | None = None,
    ) -> None:
        """将 ReAct 工具调用轮次（assistant tool_calls + tool result）持久化到 message 表。

        这是实现工具调用历史跨轮次恢复的核心方法（第一性原理方案）。
        大模型的上下文窗口是其唯一工作内存：将工具调用轮次持久化，
        等价于 LangGraph Checkpointer 的 ReAct messages 持久化，
        使大模型在步数限制/超时中断后能通过完整的 messages[] 无缝续接。

        Args:
            conversation_id: 会话 ID
            case_id: 工单 ID
            tool_calls_msg: OpenAI assistant message（含 tool_calls 数组）
            tool_result_msg: OpenAI tool message（role=tool，含 tool_call_id 和 content）
            exec_id: 工具执行 ID（可选，写入 metadata 供审计追踪）
        """
        import json as _json

        trace_id = get_current_trace_id()
        # 提取 tool_call_id 用于 tool_result 行的关联
        tool_call_id = tool_result_msg.get("tool_call_id", "")

        try:
            if self.session_factory:
                async with self.session_factory() as session:
                    repo = ConversationRepository(session)
                    # 1. 写入 tool_call 行（存储序列化后的 assistant message JSON）
                    await repo.add_message(
                        conversation_id=conversation_id,
                        case_id=case_id,
                        role=MessageRole.tool_call,
                        content=_json.dumps(tool_calls_msg, ensure_ascii=False),
                        trace_id=trace_id,
                        metadata={"exec_id": exec_id} if exec_id else {},
                    )
                    # 2. 写入 tool_result 行（存储工具输出文本，关联 tool_call_id）
                    await repo.add_message(
                        conversation_id=conversation_id,
                        case_id=case_id,
                        role=MessageRole.tool_result,
                        content=tool_result_msg.get("content", ""),
                        trace_id=trace_id,
                        tool_call_id=tool_call_id,
                        metadata={"exec_id": exec_id} if exec_id else {},
                    )
                    await session.commit()
                    logger.debug(
                        event="tool_turn_persisted",
                        message="工具调用轮次已持久化到 message 表",
                        conversation_id=str(conversation_id),
                        tool_call_id=tool_call_id,
                        exec_id=exec_id,
                    )
            else:
                logger.warning(
                    event="tool_turn_persist_skipped",
                    message="session_factory 未注入，跳过工具调用轮次持久化",
                    conversation_id=str(conversation_id),
                )
        except Exception as e:
            logger.error(
                event="save_tool_turn_error",
                message="工具调用轮次持久化失败（非阻塞，不影响主流程）",
                conversation_id=str(conversation_id),
                tool_call_id=tool_call_id,
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
        return "htp-agent"

    def _get_fallback_endpoint(self, assistant_type: str) -> str | None:
        cfg = settings.assistant_registry.get(assistant_type, {})
        endpoint = cfg.get("base_url")
        if endpoint:
            return str(endpoint).rstrip("/")
        return settings.LLM_BASE_URL.rstrip("/")

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
            case_id,
            assistant_type,
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

    async def _validate_s0_category(self, category_info: dict[str, str]) -> dict[str, str] | None:
        """用 KB 权威分类目录校验并规范化用户选中的分类。"""
        code = str(category_info.get("code") or "").strip()
        if not code or self.kb_client is None:
            logger.error(
                event="s0_category_authority_unavailable",
                message="S0 分类确认缺少 KB 权威分类客户端，拒绝 Fail Open",
                category_id=code,
            )
            return None

        try:
            grouped = await self.kb_client.get_categories_grouped(leaf_only=True)
        except Exception as exc:
            logger.error(
                event="s0_category_authority_error",
                message=f"S0 权威分类目录请求失败，拒绝推进：{exc}",
                category_id=code,
                error=str(exc),
            )
            return None
        if not grouped:
            logger.error(
                event="s0_category_authority_empty",
                message="S0 权威分类目录为空，拒绝推进 S1",
                category_id=code,
            )
            return None

        for items in grouped.values():
            for item in items:
                registered_code = str(item.get("code") or item.get("id") or "").strip()
                if registered_code != code:
                    continue
                registered_name = str(item.get("name") or item.get("label") or "").strip()
                if not registered_name:
                    return None
                return {"code": registered_code, "name": registered_name}

        logger.warning(
            event="s0_unknown_category_rejected",
            message=f"用户选择的分类 {code} 不在当前启用目录中，拒绝推进",
            category_id=code,
        )
        return None

    async def _commit_s0_confirmation(
        self,
        conversation_id: uuid.UUID,
        case_id: str,
        category_info: dict[str, str],
    ) -> bool:
        """原子提交 S0 分类与 S1 阶段，再执行幂等外部副作用。"""
        from shared.models.conversation import Conversation as ConversationModel
        from sqlalchemy import select
        from sqlalchemy import update as sa_update

        code = category_info["code"]
        name = category_info["name"]
        category_l1 = code.split("-", 1)[0]
        already_hit = False
        try:
            if self.session_factory:
                async with self.session_factory() as session:
                    duplicate = await session.execute(
                        select(ConversationModel.conversation_id)
                        .where(
                            ConversationModel.case_id == case_id,
                            ConversationModel.category_id == code,
                            ConversationModel.conversation_id != conversation_id,
                        )
                        .limit(1)
                    )
                    already_hit = duplicate.scalar_one_or_none() is not None
                    result = await session.execute(
                        sa_update(ConversationModel)
                        .where(
                            ConversationModel.conversation_id == conversation_id,
                            ConversationModel.diagnostic_stage == "S0",
                        )
                        .values(
                            category_id=code,
                            category_l1=category_l1,
                            category_l2=name,
                            diagnostic_stage="S1",
                        )
                    )
                    if result.rowcount != 1:
                        await session.rollback()
                        logger.warning(
                            event="s0_confirmation_state_conflict",
                            message="S0 分类确认时会话阶段已变化，拒绝重复推进",
                            conversation_id=str(conversation_id),
                            category_id=code,
                        )
                        return False
                    await session.commit()
            else:
                conv = await self.repository.get_conversation(conversation_id)
                if not conv or conv.diagnostic_stage != "S0":
                    return False
                conv.category_id = code
                conv.category_l1 = category_l1
                conv.category_l2 = name
                conv.diagnostic_stage = "S1"
                await self.repository.session.flush()

            logger.info(
                event="s0_confirmation_committed",
                message=f"S0 分类与阶段已原子提交：{code} {name}",
                conversation_id=str(conversation_id),
                case_id=case_id,
                category_id=code,
                old_stage="S0",
                new_stage="S1",
            )

            if self.kb_client and not already_hit:
                try:
                    hit_count = await self.kb_client.increment_category_hit(code)
                    logger.info(
                        event="category_hit_count_updated",
                        message=f"分类命中计数已更新：{code} -> {hit_count}",
                        code=code,
                        hit_count=hit_count,
                    )
                except Exception as exc:
                    # category/stage 已提交，统计副作用失败不得伪报主事务失败。
                    logger.warning(
                        event="category_hit_count_update_failed",
                        message=f"分类命中计数更新失败：{exc}",
                        code=code,
                        error=str(exc),
                    )

            if case_id and settings.CASE_SERVICE_URL:
                import httpx  # noqa: PLC0415

                try:
                    async with httpx.AsyncClient(timeout=5.0) as client:
                        response = await client.put(f"{settings.CASE_SERVICE_URL}/api/cases/{case_id}/confirm")
                    if response.status_code not in (200, 404):
                        logger.warning(
                            event="case_confirm_failed",
                            case_id=case_id,
                            status_code=response.status_code,
                        )
                    else:
                        logger.info(
                            event="case_confirmed_by_s0",
                            message=f"工单 {case_id} 已由 S0 分类写库触发 confirm",
                            case_id=case_id,
                        )
                except Exception as exc:
                    logger.warning(event="case_confirm_error", case_id=case_id, error=str(exc))
            return True
        except Exception as exc:
            logger.error(
                event="s0_confirmation_commit_error",
                message=f"S0 分类与阶段提交失败：{exc}",
                conversation_id=str(conversation_id),
                case_id=case_id,
                category_id=code,
                error=str(exc),
            )
            return False

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
        from shared.models.conversation import Conversation as ConversationModel
        from sqlalchemy import update as sa_update

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
        from shared.models.conversation import Conversation as ConversationModel
        from sqlalchemy import select
        from sqlalchemy import update as sa_update

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
                        resp = await client.put(f"{settings.CASE_SERVICE_URL}/api/cases/{case_id}/confirm")
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
        from shared.models.conversation import Conversation as ConversationModel
        from sqlalchemy import select
        from sqlalchemy import update as sa_update

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

    async def _record_tool_call(
        self,
        conversation_id: uuid.UUID,
        case_id: str,
        stage: str,
        metadata: dict[str, Any],
    ) -> None:
        """
        以 exec_id 为稳定主键记录所有 Agent 的工具生命周期。

        pai-agent 工具调用事件包含：
          - stage="tool_call": 工具调用开始，记录 tool_name、args、status="pending"
          - stage="tool_result": 工具调用完成，记录 tool_name、result、status="completed"/"error"

        Args:
            conversation_id: 会话 ID
            case_id: 工单 ID
            stage: 事件阶段（tool_call / tool_result）
            metadata: 事件元数据（tool_name, tool_args/tool_result, status 等）
        """
        from datetime import UTC, datetime

        from shared.models.audit import ToolResult
        from sqlalchemy import text

        tool_name = metadata.get("tool_name", "")
        if not tool_name:
            logger.warning(
                event="pai_tool_call_missing_name",
                message="pai-agent 工具调用事件缺少 tool_name",
                conversation_id=str(conversation_id),
                stage=stage,
            )
            return

        raw_exec_id = str(metadata.get("exec_id") or "").strip()
        if raw_exec_id:
            try:
                record_id = str(uuid.UUID(raw_exec_id))
            except ValueError:
                record_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"tool-exec:{raw_exec_id}"))
        else:
            # 旧事件没有 exec_id 时仍可审计，但无法提供跨事件的严格关联。
            record_id = str(uuid.uuid4())

        try:
            if self.session_factory:
                async with self.session_factory() as session:
                    # 根据阶段决定写入内容
                    if stage == "tool_call":
                        record = await session.get(ToolResult, record_id)
                        if record is None:
                            record = ToolResult(
                                id=record_id,
                                conversation_id=conversation_id,
                                case_id=case_id,
                                tool_name=tool_name,
                                tool_type="acli" if tool_name.startswith(("qkv_", "qfk_", "acli")) else "scp_api",
                                risk_level=int(metadata.get("risk_level") or 1),
                                policy=str(metadata.get("policy") or "auto"),
                                input_json=metadata.get("args") or metadata.get("tool_args") or {},
                                output_json=None,
                                error=None,
                                status=str(metadata.get("status") or "running"),
                                started_at=datetime.now(UTC),
                                completed_at=None,
                                duration_ms=None,
                                trace_id=get_current_trace_id(),
                                exec_id=raw_exec_id or record_id,
                            )
                            session.add(record)
                        else:
                            record.input_json = metadata.get("args") or metadata.get("tool_args") or {}
                            record.status = str(metadata.get("status") or "running")
                            record.exec_id = raw_exec_id or record_id
                        await session.commit()
                        logger.info(
                            event="tool_call_started",
                            message=f"工具调用开始：{tool_name}",
                            conversation_id=str(conversation_id),
                            tool_name=tool_name,
                            exec_id=record_id,
                            tool_args=record.input_json,
                        )

                    elif stage == "tool_result":
                        status = str(metadata.get("status") or "failed")
                        if str(metadata.get("outcome") or "").lower() == "blocked" or status == "blocked":
                            status = "blocked"
                        tool_result = metadata.get("result", metadata.get("tool_result"))
                        error = metadata.get("error")
                        artifact = None
                        if raw_exec_id:
                            artifact_result = await session.execute(
                                text(
                                    """
                                    SELECT artifact_id, trace_id, stdout_sha256, error_type, duration_ms
                                    FROM bridge_execution_artifacts
                                    WHERE exec_id = :exec_id
                                    """
                                ),
                                {"exec_id": raw_exec_id},
                            )
                            artifact = artifact_result.mappings().first()
                        record = await session.get(ToolResult, record_id)
                        now = datetime.now(UTC)
                        if record is None:
                            record = ToolResult(
                                id=record_id,
                                conversation_id=conversation_id,
                                case_id=case_id,
                                tool_name=tool_name,
                                tool_type="acli" if tool_name.startswith(("qkv_", "qfk_", "acli")) else "scp_api",
                                risk_level=int(metadata.get("risk_level") or 1),
                                policy=str(metadata.get("policy") or "auto"),
                                input_json=metadata.get("args") or metadata.get("tool_args") or {},
                                started_at=now,
                                trace_id=get_current_trace_id(),
                                exec_id=raw_exec_id or record_id,
                            )
                            session.add(record)
                        record.output_json = tool_result
                        record.error = error
                        record.completed_at = now
                        record.duration_ms = (
                            int(artifact["duration_ms"])
                            if artifact and artifact.get("duration_ms") is not None
                            else int((now - record.started_at).total_seconds() * 1000)
                        )
                        record.status = status
                        record.exec_id = raw_exec_id or record_id
                        if artifact:
                            record.artifact_id = artifact.get("artifact_id")
                            record.output_sha256 = artifact.get("stdout_sha256")
                            record.error_type = artifact.get("error_type")
                            record.bridge_trace_id = artifact.get("trace_id")
                        elif status == "blocked":
                            record.error_type = str(metadata.get("error_type") or "blocked_dependency")
                        await session.commit()
                        logger.info(
                            event="tool_call_completed",
                            message=f"工具调用完成：{tool_name}",
                            conversation_id=str(conversation_id),
                            tool_name=tool_name,
                            exec_id=record_id,
                            status=status,
                            duration_ms=record.duration_ms,
                        )

        except Exception as e:
            logger.warning(
                event="tool_call_record_error",
                message=f"工具调用记录失败：{e}",
                conversation_id=str(conversation_id),
                tool_name=tool_name,
                stage=stage,
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
        from shared.models.conversation import Conversation as ConversationModel
        from sqlalchemy import select
        from sqlalchemy import update as sa_update

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

    async def _get_last_assistant_message(
        self,
        conversation_id: uuid.UUID,
    ) -> tuple[str | None, dict | None]:
        """
        获取最后一条 assistant 消息的内容和 metadata（统一入口）。

        Args:
            conversation_id: 对话 ID

        Returns:
            tuple (content, metadata)：内容字符串和 metadata 字典，无消息时返回 (None, None)
        """
        from sqlalchemy import select

        from ..models.message import Message as MessageModel

        try:
            if self.session_factory:
                async with self.session_factory() as session:
                    result = await session.execute(
                        select(MessageModel.content, MessageModel.metadata_)
                        .where(
                            MessageModel.conversation_id == conversation_id,
                            MessageModel.role == "assistant",
                        )
                        .order_by(MessageModel.created_at.desc())
                        .limit(1)
                    )
                    row = result.fetchone()
                    return (row[0] if row else None, row[1] if row else None)
            else:
                msgs = await self.repository.get_messages(conversation_id)
                ai_msgs = [m for m in msgs if m.role.value == "assistant"]
                if not ai_msgs:
                    return (None, None)
                last_msg = ai_msgs[-1]
                return (last_msg.content, last_msg.metadata)
        except Exception as e:
            logger.warning(
                event="get_last_assistant_message_error",
                message=f"获取最后一条 AI 消息失败：{e}",
                conversation_id=str(conversation_id),
            )
            return (None, None)

    async def _extract_s0_candidates(
        self,
        conversation_id: uuid.UUID,
    ) -> list[dict[str, str]]:
        """
        从上一条 assistant 消息中提取 S0 给出的候选分类列表。

        优先级：
          1. 从 metadata 结构化元数据提取（100% 精准，0 正则）
          2. 从消息内容正则提取（兜底，支持历史消息）

        Args:
            conversation_id: 对话 ID

        Returns:
            list[dict]，每项格式 {"option_id": "...", "code": "...", "name": "..."}；
            无匹配时返回 []
        """
        # 正则：支持多级分类前缀和包含括号等特殊字符的名称
        _candidate_item_pattern = re.compile(r"([①②③④⑤])\s*([\u4e00-\u9fa5A-Za-z0-9-]+-\d+)\s+([^\n]+?)(?:\r?\n|$)")

        # 统一入口：获取最后一条 assistant 消息
        last_ai_content, last_ai_metadata = await self._get_last_assistant_message(conversation_id)

        # 1. 优先从 metadata 结构化元数据提取（方案1）
        if last_ai_metadata and isinstance(last_ai_metadata, dict):
            # 兼容三种 metadata 结构：
            #   - options 列表（落库的实际前端交互渲染结构）
            #   - 直接 candidates 字段
            #   - event.metadata.candidates 嵌套结构
            options = last_ai_metadata.get("options")
            if options and isinstance(options, list):
                extracted = []
                for opt in options:
                    if not isinstance(opt, dict):
                        continue
                    name_str = opt.get("name", "")
                    if "以上不是" in name_str or "以上都不是" in name_str:
                        continue
                    option_id = str(opt.get("optionId") or "").strip()
                    code = str(opt.get("code") or "").strip()
                    category_name = str(opt.get("categoryName") or "").strip()
                    if not code:
                        m = re.match(r"^([\u4e00-\u9fa5A-Za-z0-9-]+-\d+)\s+(.+)$", name_str.strip())
                        if not m:
                            continue
                        code = m.group(1).strip()
                        category_name = m.group(2).strip()
                    if not category_name:
                        category_name = name_str.removeprefix(code).strip()
                    extracted.append(
                        {
                            "option_id": option_id or code,
                            "code": code,
                            "name": category_name,
                        }
                    )
                if extracted:
                    logger.info(
                        event="extract_s0_candidates_metadata_success",
                        message=f"通过 metadata.options 结构化提取 {len(extracted)} 个候选分类",
                        conversation_id=str(conversation_id),
                        candidate_count=len(extracted),
                        source="metadata_options",
                    )
                    return extracted

            event_data = last_ai_metadata.get("event") or {}
            candidates_from_meta = last_ai_metadata.get("candidates") or event_data.get("metadata", {}).get(
                "candidates"
            )
            if candidates_from_meta and isinstance(candidates_from_meta, list):
                extracted = [
                    {
                        "option_id": str(c.get("optionId") or c.get("code") or ""),
                        "code": c.get("code", ""),
                        "name": c.get("name", ""),
                    }
                    for c in candidates_from_meta
                    if c and c.get("code") and c.get("name")
                ]
                logger.info(
                    event="extract_s0_candidates_metadata_success",
                    message=f"通过 metadata 结构化提取 {len(extracted)} 个候选分类",
                    conversation_id=str(conversation_id),
                    candidate_count=len(extracted),
                    source="metadata",
                )
                return extracted
            else:
                logger.debug(
                    event="extract_s0_candidates_metadata_empty",
                    message="metadata 中无候选列表，退避到正则提取",
                    conversation_id=str(conversation_id),
                )

        # 2. 兜底：从消息内容正则提取
        if not last_ai_content:
            logger.debug(
                event="extract_s0_candidates_no_message",
                message="无最后一条 AI 消息",
                conversation_id=str(conversation_id),
            )
            return []

        candidates: list[dict[str, str]] = []
        for m in _candidate_item_pattern.finditer(last_ai_content):
            circle = m.group(1)
            option_id = str("①②③④⑤".index(circle) + 1)
            candidates.append(
                {
                    "option_id": option_id,
                    "code": m.group(2).strip(),
                    "name": m.group(3).strip(),
                }
            )

        if candidates:
            logger.info(
                event="extract_s0_candidates_regex_success",
                message=f"通过正则提取 {len(candidates)} 个候选分类",
                conversation_id=str(conversation_id),
                candidate_count=len(candidates),
                source="regex",
            )
        else:
            logger.debug(
                event="extract_s0_candidates_regex_empty",
                message="正则提取未匹配到候选分类",
                conversation_id=str(conversation_id),
            )

        return candidates

    async def _get_s0_candidate_rounds(self, conversation_id: uuid.UUID) -> int:
        """
        读取 S0 候选确认已进行的轮次数。

        从 conversation.metadata_["s0_candidate_rounds"] 读取，默认 0。
        """
        from shared.models.conversation import Conversation as ConversationModel
        from sqlalchemy import select

        try:
            if self.session_factory:
                async with self.session_factory() as session:
                    result = await session.execute(
                        select(ConversationModel.metadata_).where(ConversationModel.conversation_id == conversation_id)
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
        from shared.models.conversation import Conversation as ConversationModel
        from sqlalchemy import update as sa_update

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
        from shared.models.conversation import Conversation as ConversationModel
        from sqlalchemy import update as sa_update

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
        from shared.models.conversation import Conversation as ConversationModel
        from sqlalchemy import update as sa_update

        from ..models.diagnostic_item import STATUS_ARCHIVED, DiagnosticItem

        # 获取动作描述（纯函数，不含副作用）
        action = self._conversation_manager.handle_resolution_choice(choice)  # type: ignore[arg-type]

        conv = await self.repository.get_conversation(conversation_id)
        if conv is None:
            raise ValueError(f"会话不存在：{conversation_id}")

        # 约束 1：resolved 只能在有 pending_resolution 的情况下发起
        if action["case_status"] == "resolved" and conv.pending_resolution is None:
            raise ValueError(
                "约束违反：选 A 要求 pending_resolution 非 NULL，但当前为 NULL，请先调用 send_s6_resolution_options()"
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
        kind: str,
        request_id: str,
        acp_session_id: str,
        outcome: dict,
        metadata: dict | None = None,
    ) -> bool:
        """T-E6 + T-AGT-03: 将用户对交互卡片的响应回传给 agent-service。

        按 kind 字段分叉路由：
        - kind=tool_confirm → 调用 agent-service /v1/agent/react-confirm（ReAct 确认回路）
        - kind=variable_input/variable_confirm → 写入 SOP 变量池（HTP 排障）
        - kind=human_escalation → 本地记录用户已知悉，不转发给任何 Agent
        - 其他 ACP 类型 → 调用 agent-service /v1/agent/interactive-response（ops-agent ACP）

        Args:
            conversation_id: 对话 ID（用于日志追踪）。
            kind:            交互类型（tool_confirm / human_escalation / sop_step / info_confirm / variable_input 等）。
            request_id:      ACP request_id（来自 AgentInteractiveRequest.request_id）。
            acp_session_id:  ops-agent ACP session_id（来自 AgentInteractiveRequest.acp_session_id）。
            outcome:         提交结果，格式 {"outcome": "selected", "optionId": "A"}
                             或 {"outcome": "free_text", "text": "..."}
                             或 {"confirmed": true, "authorized_by": "user"}（tool_confirm）。
            metadata:        可选元数据（包含变量名等）。

        Returns:
            True  = 提交成功；False = AgentClient 未注入或请求失败。
        """
        session_id = str(conversation_id)

        # AgentEscalation 由 HTP Agent 在本地发出；其 request_id/acp_session_id
        # 仅用于前端卡片关联，并不是 ops-agent 创建的 ACP 请求。确认“我知道了”
        # 只需在会话历史中留痕，绝不能转发到 ops-agent。
        if kind == "human_escalation":
            if outcome.get("outcome") != "selected" or outcome.get("optionId") != "ack":
                logger.warning(
                    event="human_escalation_ack_invalid",
                    message="人工升级确认仅接受 ack 选项",
                    conversation_id=str(conversation_id),
                    request_id=request_id,
                    outcome=outcome,
                )
                return False
            success = True
            logger.info(
                event="human_escalation_acknowledged",
                message="用户已确认人工升级提示；不转发 ops-agent",
                conversation_id=str(conversation_id),
                request_id=request_id,
            )
        elif self._agent_client is None:
            logger.warning(
                event="interactive_response_no_client",
                message="submit_interactive_response: AgentClient 未注入，跳过",
                conversation_id=str(conversation_id),
                kind=kind,
                request_id=request_id,
            )
            return False

        # 按 kind 分叉路由
        elif kind in ("variable_input", "variable_confirm"):
            # ── SOP 变量输入/确认路径 ──
            if not metadata or "variable_name" not in metadata:
                logger.warning(
                    event="sop_variable_response_missing_metadata",
                    message="SOP 变量提交缺少 metadata 或 variable_name",
                    conversation_id=str(conversation_id),
                    kind=kind,
                )
                return False

            variable_name = metadata["variable_name"]
            value = None
            if outcome.get("outcome") == "free_text":
                value = outcome.get("text")
            elif outcome.get("outcome") == "selected":
                value = outcome.get("optionId") or outcome.get("optionLabel")
            else:
                value = outcome.get("value") or outcome.get("text")

            if value is None:
                logger.warning(
                    event="sop_variable_response_missing_value",
                    message="SOP 变量提交缺少值",
                    conversation_id=str(conversation_id),
                    variable_name=variable_name,
                )
                return False

            if self.session_factory:
                async with self.session_factory() as session:
                    repo = SopExecutionRepository(session)
                    updated = await repo.set_variable(
                        conversation_id=conversation_id,
                        variable_name=variable_name,
                        value=str(value),
                        source="user_input",
                    )
                    if updated:
                        await session.commit()
                        logger.info(
                            event="sop_variable_submitted_via_interactive",
                            message="SOP 变量已通过交互卡片写入",
                            conversation_id=str(conversation_id),
                            variable_name=variable_name,
                            value=value,
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
                                    metadata={
                                        "kind": "interactive_response",
                                        "selectedOptionId": outcome.get("optionId")
                                        if outcome.get("outcome") == "selected"
                                        else None,
                                    },
                                )
                        except Exception as msg_err:
                            logger.warning(f"SOP 变量提交写入 user 消息失败: {msg_err}")
                        return True
                    else:
                        logger.warning(
                            event="sop_variable_submit_no_execution",
                            message="SOP 变量提交失败：未找到对应的 SOP 执行实例",
                            conversation_id=str(conversation_id),
                            variable_name=variable_name,
                        )
                        return False
            else:
                logger.warning(
                    event="sop_variable_submit_no_session_factory",
                    message="SOP 变量提交失败：session_factory 未注入",
                    conversation_id=str(conversation_id),
                )
                return False

        elif kind == "tool_confirm":
            # ── ReAct 确认回路（T-AGT-03）────────────────────────────────────────────
            confirmed = bool(outcome.get("confirmed", False))
            authorized_by = outcome.get("authorized_by", "user")
            input_hash = outcome.get("input_hash") or (metadata.get("input_hash") if metadata else None)

            if not request_id:
                logger.warning(
                    event="react_confirm_missing_request_id",
                    message="ReAct 工具确认提交缺失 request_id (exec_id)",
                    conversation_id=str(conversation_id),
                )
                return False

            if not self.session_factory:
                logger.error(
                    event="react_confirm_no_session_factory",
                    message="工具确认缺少数据库 session_factory，无法校验 exec_id/input_hash，已拒绝继续提交确认",
                    conversation_id=str(conversation_id),
                )
                return False

            from datetime import UTC, datetime, timedelta

            from shared.models.audit import Authorization, ToolResult
            from sqlalchemy import select

            try:
                async with self.session_factory() as session:
                    stmt = select(ToolResult).where(ToolResult.id == request_id)
                    res = await session.execute(stmt)
                    tool_res = res.scalar_one_or_none()

                    if not tool_res:
                        logger.warning(
                            event="react_confirm_record_not_found",
                            message=f"未找到对应的 tool_result 记录, request_id={request_id}",
                            conversation_id=str(conversation_id),
                        )
                        return False

                    if tool_res.input_hash and (not input_hash or tool_res.input_hash != input_hash):
                        logger.error(
                            event="react_confirm_hash_mismatch",
                            message="工具确认参数 hash 不匹配，防篡改校验未通过！",
                            conversation_id=str(conversation_id),
                            db_hash=tool_res.input_hash,
                            client_hash=input_hash,
                        )
                        return False

                    auth_id = str(uuid.uuid4())
                    auth = Authorization(
                        auth_id=auth_id,
                        exec_id=request_id,
                        actor=authorized_by,
                        decision="approve" if confirmed else "deny",
                        tool_input_hash=input_hash or tool_res.input_hash or "",
                        expires_at=datetime.now(UTC) + timedelta(seconds=120),
                    )
                    session.add(auth)
                    tool_res.authorization_id = auth_id
                    tool_res.authorized_by = authorized_by
                    await session.commit()
            except Exception as db_exc:
                logger.error(
                    event="react_confirm_db_error",
                    message=f"工具确认审计/校验失败，已拒绝继续提交确认: {db_exc}",
                    conversation_id=str(conversation_id),
                )
                return False

            try:
                success = await self._agent_client.react_confirm(
                    session_id=session_id,
                    confirmed=confirmed,
                    authorized_by=authorized_by,
                    exec_id=request_id,
                )
            except Exception as exc:
                logger.warning(
                    event="react_confirm_error",
                    message=f"react_confirm 异常: {exc}",
                    conversation_id=str(conversation_id),
                    session_id=session_id,
                )
                return False
            logger.info(
                event="react_confirm_submitted",
                message="ReAct 工具确认已提交",
                conversation_id=str(conversation_id),
                session_id=session_id,
                confirmed=confirmed,
                authorized_by=authorized_by,
                success=success,
            )
        else:
            # ── ACP 路径（ops-agent SOP/信息确认卡）──────────────────────────────────
            try:
                success = await self._agent_client.submit_interactive_response(
                    acp_session_id=acp_session_id,
                    request_id=request_id,
                    outcome=outcome,
                )
            except Exception as exc:
                logger.warning(
                    event="interactive_response_error",
                    message=f"submit_interactive_response 异常: {exc}",
                    conversation_id=str(conversation_id),
                    request_id=request_id,
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
                        "interactive_kind": kind,
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
        1. 通过 OpsAgentAdapter.resume_event_stream() 消费 outbox
        2. 将 AgentEvent 序列化为与 send_message_stream_only 相同的 SSE 格式
        3. active_prompt=False 时立即返回，不挂起

        Yields:
            str: SSE 格式的文本/事件片段，与 send_message_stream_only 格式一致
        """
        import json as _json

        if self._agent_client is None:
            return

        # 预查 case_id，供后续落库使用
        _conv = await self.repository.get_conversation(conversation_id)
        if _conv is None:
            logger.warning(
                event="resume_stream_conv_not_found",
                message="resume_ops_agent_stream: conversation 不存在，跳过落库",
                conversation_id=str(conversation_id),
            )
        _case_id = _conv.case_id if _conv else None

        session_id = str(conversation_id)
        async for agent_event in self._agent_client.resume_stream(session_id):
            event_type = agent_event.get("type")
            if event_type == "text_chunk":
                _chunk = agent_event.get("content", "")
                if _chunk:
                    yield _chunk
            elif event_type == "interactive_request":
                _ir_payload = _json.dumps(
                    {
                        "requestId": agent_event.get("request_id"),
                        "acpSessionId": agent_event.get("acp_session_id"),
                        "kind": agent_event.get("kind"),
                        "title": agent_event.get("title"),
                        "prompt": agent_event.get("prompt"),
                        "options": agent_event.get("options"),
                        "customInput": agent_event.get("custom_input"),
                        "metadata": agent_event.get("metadata"),
                        "execId": agent_event.get("exec_id"),
                        "inputHash": agent_event.get("input_hash"),
                        "expiresAt": agent_event.get("expires_at"),
                    },
                    ensure_ascii=False,
                )
                yield f"\x00event:interactive_request:{_ir_payload}\x00"
                # 交互请求落库（conv 不存在时跳过，避免 case_id='' 脏数据）
                if _case_id is not None:
                    asyncio.create_task(
                        self._save_message_bg(
                            conversation_id=conversation_id,
                            case_id=_case_id,
                            role=MessageRole.assistant,
                            content=self._format_interactive_request_content_dict(agent_event),
                            metadata={
                                "kind": "interactive_request",
                                "event": {
                                    "requestId": agent_event.get("request_id"),
                                    "acpSessionId": agent_event.get("acp_session_id"),
                                    "kind": agent_event.get("kind"),
                                    "title": agent_event.get("title"),
                                    "prompt": agent_event.get("prompt"),
                                    "options": agent_event.get("options"),
                                    "customInput": agent_event.get("custom_input"),
                                    "metadata": agent_event.get("metadata"),
                                    "execId": agent_event.get("exec_id"),
                                    "inputHash": agent_event.get("input_hash"),
                                    "expiresAt": agent_event.get("expires_at"),
                                },
                            },
                        )
                    )
            elif event_type == "escalation":
                _escalation_event = self._escalation_interactive_event(conversation_id, agent_event)
                yield f"\x00event:interactive_request:{_json.dumps(_escalation_event, ensure_ascii=False)}\x00"
            elif event_type == "stage_update":
                _stage = agent_event.get("stage", "")
                _metadata = agent_event.get("metadata", {})
                yield f"\x00event:stage_change:{_stage}\x00"
                if _stage in ("tool_call", "tool_result"):
                    if _case_id is not None:
                        await self._record_tool_call(
                            conversation_id=conversation_id,
                            case_id=_case_id,
                            stage=_stage,
                            metadata=_metadata,
                        )
                    _payload = _json.dumps(_metadata, ensure_ascii=False)
                    yield f"\x00event:{_stage}:{_payload}\x00"
            elif event_type == "done":
                break
