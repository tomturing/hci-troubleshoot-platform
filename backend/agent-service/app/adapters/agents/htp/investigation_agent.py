"""
InvestigationAgent: S1-S4 诊断调查 Agent（继承 BaseAgent）

职责：
  - 从知识库检索含结构化步骤的候选案例（top-K=15）
  - 执行案例差异诊断（CDD）贪心消除算法
  - 流式输出诊断进展（步骤执行、阶段更新）
  - 生成结构化诊断报告

执行模式（T-AGT-22 统一后）：
  sop    → ReactEngine + SOP 导航工具注入（动态获取节点内容）
  kbd/无 → CDD 模式：案例差异诊断，结构化匹配

设计：
  - think()：根据当前 CDD 状态决定下一步工具调用（ToolCall），
             或在锁定案例后返回诊断报告（str）
  - act()：执行 ToolExecutor，返回观察结果
  - process()：CDD 驱动的完整诊断流程，含流式事件
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from typing import Any

from shared.clients import AIAssistantRegistry, DiagnosticItemClient, KBClient
from shared.observability.logger import get_logger

from app.adapters.agents.htp.kbd_differential import KBDDiagnostic
from app.adapters.agents.htp.kbd_model import KBD, kbd_from_dict
from app.adapters.agents.htp.react_engine import ReactEngine
from app.adapters.agents.htp.sop_tools import (
    ConversationSopClient,
    SopToolExecutor,
    get_sop_node,
)
from app.domain.agent_port import (
    AgentEvent,
    AgentInteractiveRequest,
    AgentStageUpdate,
    AgentTextChunk,
    AgentUnavailableError,
)
from app.domain.base_agent import BaseAgent, Message, Observation, Step, ToolCall

logger = get_logger("investigation-agent")

# CDD 候选案例检索数量
DEFAULT_TOP_K = 15

# SOP 内容截断上限（防止超出 LLM 上下文窗口）
MAX_SOP_CHARS = 8000

# SOP 根节点 ID（默认）
DEFAULT_ROOT_NODE_ID = "n-1"


class InvestigationAgent(BaseAgent):
    """S1-S4 诊断调查 Agent（CDD 驱动）。

    核心流程：
      1. 检索 top-K 候选案例（含结构化步骤）
      2. 若找到 SOP → ReactEngine + SOP 导航工具注入（T-AGT-22）
      3. 若找到案例 → CDD 贪心消除 → 生成报告
      4. 若无知识 → 机制推理模式（LLM 自由推理）
    """

    def __init__(
        self,
        ai_registry: AIAssistantRegistry,
        kb_client: KBClient,
        tool_executor: Any,  # 实现 ToolExecutor Protocol
        diagnostic_item_client: DiagnosticItemClient | None = None,  # T-AGT-19: 用于插入诊断条目
        conversation_service_url: str = "",  # T-AGT-22: 用于创建/推进 SOP 执行
        internal_token: str = "",  # T-AGT-22: 内部服务认证 Token
        top_k: int = DEFAULT_TOP_K,
        confirm_service: Any = None,
        audit_service: Any = None,
        db_session_factory: Any = None,
        fact_store: Any = None,
        skill_runner: Any | None = None,
    ) -> None:
        super().__init__(name="investigation-agent", max_steps=20)
        self._ai_registry = ai_registry
        self._kb_client = kb_client
        self._tool_executor = tool_executor
        self._diagnostic_item_client = diagnostic_item_client
        self._conversation_service_url = conversation_service_url
        self._internal_token = internal_token
        self._top_k = top_k
        self._confirm_service = confirm_service
        self._audit_service = audit_service
        if db_session_factory is None:
            from shared.utils.prompt_loader import create_mock_session_factory

            self._db_session_factory = create_mock_session_factory()
        else:
            self._db_session_factory = db_session_factory

        from app.services.evidence_builder import EvidenceBuilder

        self._evidence_builder = EvidenceBuilder(fact_store=fact_store)
        self._fact_store = fact_store
        self._skill_runner = skill_runner

        # KBD 差异诊断引擎（每次 process() 调用时重新创建，保证状态隔离）
        self._kbd_diag: KBDDiagnostic | None = None

    # ─── BaseAgent 抽象方法实现 ─────────────────────────────────────────────────

    async def think(self, context: list[Message]) -> Step:
        """根据当前 CDD 状态决定下一步（仅在 run() 内部调用）。

        InvestigationAgent 的控制逻辑主要在 process() 中实现（CDD 驱动），
        此方法保留为 BaseAgent 协议的实现，供非流式路径使用。
        """
        # KBD 诊断已完成：返回最终报告
        if self._kbd_diag and self._kbd_diag.get_result():
            result = self._kbd_diag.get_result()
            return result.diagnosis_report if result else "诊断完成，请查看报告"
        return "诊断进行中"

    async def act(self, tool_call: ToolCall) -> Observation:
        """执行工具调用（由 CDD 引擎内部协调，外部调用应使用 process()）。"""
        try:
            result = await self._tool_executor.execute(tool_call.name, tool_call.args)
            return Observation(tool_call=tool_call, result=result, error=None)
        except Exception as exc:
            return Observation(tool_call=tool_call, result=None, error=exc)

    # ─── 对外接口（供 AgentRouter 调用）───────────────────────────────────────

    async def process(
        self,
        *,
        session_id: str,
        messages: list[dict[str, Any]],
        category_id: str,
        diagnostic_stage: str = "S1",
        env_context: dict[str, Any] | None = None,
        assistant_type: str = "htp-agent",
        case_id: str = "",
        user_id: str = "",
        execution_mode: str = "safe-only",
        sop_resume_context: dict[str, Any] | None = None,  # T-AGT-23: SOP 执行恢复上下文
    ) -> AsyncGenerator[AgentEvent, None]:
        """S1-S4 诊断调查完整流程（流式）。

        Args:
            session_id: 会话 ID
            messages: OpenAI 格式消息列表（对话历史）
            category_id: S0 确认的故障分类编码，如 "虚拟机-003"
            diagnostic_stage: 当前阶段（S1/S2/S3/S4）
            env_context: 环境上下文（含 vm_name、host_id 等占位符替换所需键值）
            assistant_type: 助手类型标识
            case_id: 工单 ID
            user_id: 用户 ID
            sop_resume_context: SOP 执行恢复上下文（T-AGT-23，用于断线重连恢复）

        Yields:
            AgentStageUpdate(stage="investigation_start") — 开始
            AgentStageUpdate(stage="cdd_*")               — CDD 步骤事件
            AgentTextChunk                                — 诊断报告文本
            AgentStageUpdate(stage="S4")                  — 根因确认，推进阶段
        """
        ai_client = self._ai_registry.get_client(assistant_type)
        if not ai_client:
            raise AgentUnavailableError(
                agent_name="investigation-agent",
                reason=f"未找到助手类型 '{assistant_type}'",
            )

        # T2-4: 信息质量检查与澄清拦截（仅在非 SOP 恢复场景下触发）
        if not sop_resume_context:
            quality_report = await self._evidence_builder.check_information_quality(
                session_id=session_id,
                env_context=env_context,
            )
            if quality_report.needs_clarification:
                logger.warning(
                    event="information_quality_low",
                    message="环境数据质量不足，发起用户澄清",
                    session_id=session_id,
                    score=quality_report.quality_score,
                    missing=quality_report.missing_keys,
                )
                yield AgentInteractiveRequest(
                    request_id=f"clarify-{session_id}",
                    acp_session_id=session_id,
                    kind="information_clarification",
                    title="需要补充环境信息",
                    prompt=quality_report.to_clarification_prompt(),
                    options=[
                        {"optionId": "retry", "name": "已补充，重新检查"},
                        {"optionId": "skip", "name": "忽略，继续诊断"},
                    ],
                    custom_input=True,
                    metadata={
                        "missing_keys": quality_report.missing_keys,
                        "stale_keys": quality_report.stale_keys,
                        "low_confidence_keys": quality_report.low_confidence_keys,
                    },
                )
                return

        user_query = self._extract_user_query(messages)

        yield AgentStageUpdate(
            stage="investigation_start",
            metadata={
                "category_id": category_id,
                "diagnostic_stage": diagnostic_stage,
                "session_id": session_id,
            },
        )

        # 1. 尝试三轨路由（优先 SOP）
        track = ""
        sop_results = []

        # T-AGT-23: 如果存在活跃的 SOP 恢复上下文，直接使用已有的 SOP 路由，不再重新计算路由，防止输入内容变化导致路由漂移
        if sop_resume_context and sop_resume_context.get("sop_document_id"):
            resume_doc_id = sop_resume_context.get("sop_document_id")
            logger.info(
                event="sop_resume_bypass_route",
                message="检测到活跃的 SOP 恢复上下文，跳过三轨路由，直接使用原 SOP",
                session_id=session_id,
                sop_document_id=resume_doc_id,
            )
            try:
                doc = await self._kb_client.get_sop_document(resume_doc_id)
                if doc:
                    track = "sop"
                    sop_results = [
                        {
                            "id": doc.get("id"),
                            "title": doc.get("title"),
                            "content_md": doc.get("content_md"),
                        }
                    ]
            except Exception as e:
                logger.error(
                    event="sop_resume_fetch_document_failed",
                    message=f"恢复 SOP 时获取文档 {resume_doc_id} 失败，将尝试重新路由",
                    error=str(e),
                    session_id=session_id,
                )

        if not track:
            route_result = await self._kb_client.route_by_category(
                category_code=category_id,
                query=user_query,
                top_k=3,
            )
            track = (route_result or {}).get("track", "")
            sop_results = (route_result or {}).get("results", [])

        logger.info(
            event="investigation_route",
            track=track,
            result_count=len(sop_results),
            category_id=category_id,
            session_id=session_id,
        )

        # 2. SOP 轨道 → ReactEngine + SOP 导航工具（T-AGT-22）
        if track == "sop" and sop_results:
            sop_content = sop_results[0].get("content_md", "")
            sop_title = sop_results[0].get("title", "SOP 排障手册")
            sop_document_id = sop_results[0].get("id")
            async for event in self._process_sop_mode(
                sop_content=sop_content,
                sop_title=sop_title,
                sop_document_id=sop_document_id,
                messages=messages,
                category_id=category_id,
                diagnostic_stage=diagnostic_stage,
                ai_client=ai_client,
                case_id=case_id,
                user_id=user_id,
                session_id=session_id,  # T-AGT-22: 传递 session_id 用于创建 SopExecution
                execution_mode=execution_mode,
                sop_resume_context=sop_resume_context,  # T-AGT-23: SOP 执行恢复上下文
            ):
                yield event
            return

        # 3. KBD/无 → CDD 模式：检索含步骤的候选案例
        raw_cases = await self._kb_client.search_cases_with_steps(
            category_id=category_id,
            query=user_query,
            top_k=self._top_k,
        )

        if not raw_cases:
            # 无案例 → 机制推理降级
            async for event in self._process_fallback_mode(
                session_id=session_id,
                messages=messages,
                category_id=category_id,
                diagnostic_stage=diagnostic_stage,
                ai_client=ai_client,
                case_id=case_id,
                user_id=user_id,
            ):
                yield event
            return

        candidates: list[KBD] = [kbd_from_dict(d) for d in raw_cases]
        logger.info(
            event="kbd_diag_candidates",
            count=len(candidates),
            category_id=category_id,
            session_id=session_id,
        )

        # 4. 执行 KBD 差异诊断
        env_ctx: dict[str, str] = {
            k: str(v) for k, v in (env_context or {}).items() if isinstance(v, (str, int, float))
        }
        self._kbd_diag = KBDDiagnostic(
            ai_registry=self._ai_registry,
            tool_executor=self._tool_executor,
            diagnostic_item_client=self._diagnostic_item_client,
            conversation_id=session_id,  # T-AGT-19: 传入会话 ID 用于 INSERT
            assistant_type=assistant_type,
        )

        async for event in self._kbd_diag.diagnose(
            candidates=candidates,
            env_context=env_ctx,
            session_id=session_id,
            user_id=user_id,
        ):
            yield event

        # 5. 输出诊断报告
        kbd_result = self._kbd_diag.get_result()
        if kbd_result:
            # 流式输出报告文本
            for chunk in self._split_text_chunks(kbd_result.diagnosis_report, chunk_size=100):
                yield AgentTextChunk(content=chunk)

            # 推进到 S4（根因确认）
            matched_ids = [kbd.id for kbd in kbd_result.matched_kbds[:3]]
            yield AgentStageUpdate(
                stage="S4",
                metadata={
                    "category_id": category_id,
                    "matched_kbds": matched_ids,
                    "is_definitive": kbd_result.is_definitive,
                    "steps_count": len(kbd_result.steps_executed),
                },
            )

    # ─── 执行模式（内部）──────────────────────────────────────────────────────

    async def _process_sop_mode(
        self,
        sop_content: str,
        sop_title: str,
        sop_document_id: int | None,
        messages: list[dict],
        category_id: str,
        diagnostic_stage: str,
        ai_client: Any,
        case_id: str,
        user_id: str,
        session_id: str,  # T-AGT-22: 新增参数，用于创建 SopExecution
        execution_mode: str = "safe-only",
        sop_resume_context: dict[str, Any] | None = None,  # T-AGT-23: SOP 执行恢复上下文
    ) -> AsyncGenerator[AgentEvent, None]:
        """SOP 轨道：ReactEngine + SOP 导航工具动态注入（T-AGT-22）。

        核心流程：
          1. 检测恢复场景：若 sop_resume_context 存在，跳过创建 SopExecution，直接使用恢复信息
          2. 创建 SopExecution 记录（调用 conversation-service API，仅在非恢复场景）
          3. 获取 SOP 根节点或当前节点内容（用于构建 system prompt）
          4. 构建 system prompt（含恢复信息或初始化信息）
          5. 创建 SopToolExecutor（注入 SOP 工具上下文和 completed_steps）
          6. 调用 ReactEngine.execute()，动态注入 SOP 工具
          7. LLM 可在同一轮中调用诊断工具和 SOP 导航工具

        Args:
            sop_content: SOP 文档内容（Markdown 格式，用于构建初始 prompt）
            sop_title: SOP 文档标题
            sop_document_id: SOP 文档 ID（用于获取决策树）
            messages: 对话历史
            category_id: 故障分类编码
            diagnostic_stage: 当前诊断阶段
            ai_client: AI 客户端
            case_id: 工单 ID
            user_id: 用户 ID
            session_id: 会话 ID（用于创建 SopExecution）
            sop_resume_context: SOP 执行恢复上下文（T-AGT-23，用于断线重连恢复）
        """
        # 1. 创建 ConversationSopClient 和 SopToolExecutor
        if not self._conversation_service_url or not self._internal_token:
            logger.warning(
                event="sop_mode_fallback",
                reason="conversation_service_url 或 internal_token 未配置",
                case_id=case_id,
            )
            # 降级到旧路径（纯 chat_completion_stream）
            async for event in self._process_sop_mode_legacy(
                session_id=session_id,
                sop_content=sop_content,
                sop_title=sop_title,
                sop_document_id=sop_document_id,
                messages=messages,
                category_id=category_id,
                diagnostic_stage=diagnostic_stage,
                ai_client=ai_client,
                case_id=case_id,
                user_id=user_id,
            ):
                yield event
            return

        conversation_sop_client = ConversationSopClient(
            base_url=self._conversation_service_url,
            internal_token=self._internal_token,
        )

        # T-AGT-23: 检测恢复场景
        is_resume = sop_resume_context is not None
        current_node_id = DEFAULT_ROOT_NODE_ID
        completed_steps: list[str] = []
        context_variables: dict = {}

        if is_resume:
            # 恢复场景：直接使用 sop_resume_context 中的信息
            current_node_id = sop_resume_context.get("current_node_id", DEFAULT_ROOT_NODE_ID)
            completed_steps = sop_resume_context.get("completed_steps", [])
            context_variables = sop_resume_context.get("context_variables", {})
            sop_document_id = sop_resume_context.get("sop_document_id", sop_document_id)

            logger.info(
                event="sop_execution_resume",
                session_id=session_id,
                sop_document_id=sop_document_id,
                current_node_id=current_node_id,
                completed_steps_count=len(completed_steps),
                is_resume=True,
            )

            # 获取当前节点内容（而非根节点）
            current_node_result = await get_sop_node(
                node_id=current_node_id,
                sop_document_id=sop_document_id or 0,
                kb_client=self._kb_client,
            )

            if "error" in current_node_result:
                logger.warning(
                    event="sop_resume_current_node_failed",
                    sop_document_id=sop_document_id,
                    current_node_id=current_node_id,
                    error=current_node_result.get("error"),
                )

            # 构建恢复版 system prompt
            system_prompt = await self._build_sop_resume_prompt(
                sop_title=sop_title,
                current_node_id=current_node_id,
                completed_steps=completed_steps,
                context_variables=context_variables,
                current_node=current_node_result
                if "error" not in current_node_result
                else {
                    "node_id": current_node_id,
                    "title": "未知节点",
                    "type": "branch",
                    "content": f"无法获取当前节点时，使用 SOP 文档内容作为 fallback\n{self._truncate_sop_content(sop_content)}",
                },
                diagnostic_stage=diagnostic_stage,
                case_id=case_id,
            )
        else:
            # 新建场景：创建 SopExecution 记录
            create_result = await conversation_sop_client.create(
                conversation_id=uuid.UUID(session_id),
                sop_document_id=sop_document_id or 0,
                root_node_id=DEFAULT_ROOT_NODE_ID,
            )

            if "error" in create_result:
                logger.error(
                    event="sop_execution_create_failed",
                    session_id=session_id,
                    sop_document_id=sop_document_id,
                    error=create_result.get("error"),
                )
                # 创建失败时降级到旧路径
                async for event in self._process_sop_mode_legacy(
                    session_id=session_id,
                    sop_content=sop_content,
                    sop_title=sop_title,
                    sop_document_id=sop_document_id,
                    messages=messages,
                    category_id=category_id,
                    diagnostic_stage=diagnostic_stage,
                    ai_client=ai_client,
                    case_id=case_id,
                    user_id=user_id,
                ):
                    yield event
                return

            current_node_id = create_result.get("current_node_id", DEFAULT_ROOT_NODE_ID)
            logger.info(
                event="sop_execution_created",
                session_id=session_id,
                sop_document_id=sop_document_id,
                current_node_id=current_node_id,
                is_resume=False,
            )

            # 获取 SOP 根节点内容（用于构建 system prompt）
            root_node_result = await get_sop_node(
                node_id=DEFAULT_ROOT_NODE_ID,
                sop_document_id=sop_document_id or 0,
                kb_client=self._kb_client,
            )

            if "error" in root_node_result:
                logger.warning(
                    event="sop_root_node_failed",
                    sop_document_id=sop_document_id,
                    error=root_node_result.get("error"),
                )

            # 构建新建版 system prompt
            context_variables = create_result.get("context_variables", {})
            system_prompt = await self._build_sop_react_prompt(
                sop_title=sop_title,
                root_node=root_node_result
                if "error" not in root_node_result
                else {
                    "title": sop_title,
                    "type": "branch",
                    "content": f"无法获取根节点时，使用 SOP 文档内容作为 fallback\n{self._truncate_sop_content(sop_content)}",
                },
                diagnostic_stage=diagnostic_stage,
                case_id=case_id,
                context_variables=context_variables,
            )

        # 发送 SOP 模式启动事件（携带 sop_document_id 和恢复标记）
        yield AgentStageUpdate(
            stage="sop_reasoning",
            metadata={
                "sop_title": sop_title,
                "sop_document_id": sop_document_id,
                "category_id": category_id,
                "current_node_id": current_node_id,
                "is_resume": is_resume,  # T-AGT-23: 恢复标记
                "completed_steps_count": len(completed_steps),
            },
        )

        # 创建 SopToolExecutor 和 ReactEngine（注入 completed_steps 用于幂等性检查）
        sop_tool_executor = SopToolExecutor(
            sop_document_id=sop_document_id or 0,
            conversation_id=session_id,
            kb_client=self._kb_client,
            conversation_sop_client=conversation_sop_client,
            default_executor=self._tool_executor,
            skill_runner=self._skill_runner,
            completed_steps=completed_steps,  # T-AGT-23: 已完成节点列表（幂等性检查）
        )

        react_engine = ReactEngine(
            ai_registry=self._ai_registry,
            tool_registry={},  # tool_registry 在 ReactEngine 内部通过 get_tools_for_llm() 获取
            tool_executor=self._tool_executor,
            confirm_service=self._confirm_service,
            audit_service=self._audit_service,
            fact_store=self._fact_store,
            db_session_factory=self._db_session_factory,
        )

        # T3-2: 绑定诊断阶段结构化 Schema
        from shared.models import ClaimVerification, ReasoningOutput

        response_schema = None
        if diagnostic_stage in ("S2", "S3"):
            response_schema = ReasoningOutput
        elif diagnostic_stage == "S4":
            response_schema = ClaimVerification

        # 调用 ReactEngine.execute()，动态注入 SOP 工具
        async for event in react_engine.execute(
            session_id=session_id,
            system_prompt=system_prompt,
            messages=messages,
            assistant_type="htp-agent",
            case_id=case_id,
            user_id=user_id,
            max_iterations=40,
            execution_mode=execution_mode,
            tool_executor=sop_tool_executor,  # 使用 SOP 工具执行器
            sop_mode=True,  # DC-01: SOP 模式，注入 SOP 导航工具到 LLM tool list
            response_schema=response_schema,
        ):
            yield event

    async def _process_sop_mode_legacy(
        self,
        session_id: str,
        sop_content: str,
        sop_title: str,
        sop_document_id: int | None,
        messages: list[dict],
        category_id: str,
        diagnostic_stage: str,
        ai_client: Any,
        case_id: str,
        user_id: str,
    ) -> AsyncGenerator[AgentEvent, None]:
        """SOP 轨道降级路径：纯 chat_completion_stream（当 ReactEngine 不可用时）。

        注意：此方法为降级路径，不创建 SopExecution 记录，
        无法支持中断恢复和 SOP 导航工具。
        """
        system_prompt = await self._build_sop_prompt_legacy(
            sop_content=sop_content,
            sop_title=sop_title,
            diagnostic_stage=diagnostic_stage,
            case_id=case_id,
        )
        full_messages = [{"role": "system", "content": system_prompt}, *messages]

        yield AgentStageUpdate(
            stage="sop_reasoning",
            metadata={
                "sop_title": sop_title,
                "sop_document_id": sop_document_id,
                "category_id": category_id,
                "note": "降级路径：无 ReactEngine 支持",
            },
        )

        async for chunk in ai_client.chat_completion_stream(
            messages=full_messages,
            user_id=session_id,
            case_id=case_id,
        ):
            if chunk:
                yield AgentTextChunk(content=chunk)

    async def _process_fallback_mode(
        self,
        session_id: str,
        messages: list[dict],
        category_id: str,
        diagnostic_stage: str,
        ai_client: Any,
        case_id: str,
        user_id: str,
    ) -> AsyncGenerator[AgentEvent, None]:
        """无知识库匹配时：机制推理降级模式（流式输出）。"""
        system_prompt = await self._build_fallback_prompt(
            category_id=category_id,
            diagnostic_stage=diagnostic_stage,
            case_id=case_id,
        )
        full_messages = [{"role": "system", "content": system_prompt}, *messages]

        yield AgentStageUpdate(
            stage="mechanism_reasoning",
            metadata={"category_id": category_id, "note": "知识库无匹配，使用机制推理"},
        )

        async for chunk in ai_client.chat_completion_stream(
            messages=full_messages,
            user_id=session_id,
            case_id=case_id,
        ):
            if chunk:
                yield AgentTextChunk(content=chunk)

    # ─── Prompt 构建（内部）──────────────────────────────────────────────────

    async def _build_sop_react_prompt(
        self,
        sop_title: str,
        root_node: dict,
        diagnostic_stage: str,
        case_id: str,
        context_variables: dict | None = None,
    ) -> str:
        """构建 SOP ReactEngine 模式 System Prompt（数据库化）。"""
        stage_desc_map = {
            "S1": "S1 - 故障定位",
            "S2": "S2 - 假设生成",
            "S3": "S3 - 证据验证",
            "S4": "S4 - 根因确认",
        }
        stage_desc = stage_desc_map.get(diagnostic_stage, diagnostic_stage)

        # 已知变量（从 context_variables 提取值）
        var_summary = ""
        if context_variables:
            var_parts = []
            for var_name, var_info in context_variables.items():
                if isinstance(var_info, dict) and "value" in var_info:
                    var_parts.append(f"{var_name}={var_info['value']}")
                elif isinstance(var_info, (str, int, float)):
                    var_parts.append(f"{var_name}={var_info}")
            if var_parts:
                var_summary = f"【已知变量】\n{', '.join(var_parts)}\n\n"

        # 解析 root_node 各字段
        root_node_title = root_node.get("title", sop_title)
        root_node_type = root_node.get("type", "branch")
        root_node_content = root_node.get("content", "")
        if len(root_node_content) > 500:
            root_node_content = root_node_content[:500]

        children = root_node.get("children", [])
        branches_list = []
        for child in children[:5]:
            prereqs = child.get("prerequisites", [])
            prereq_str = f" (前置条件: {', '.join(prereqs)})" if prereqs else ""
            branches_list.append(f"- {child.get('node_id', '')}: {child.get('title', '')}{prereq_str}")
        if len(children) > 5:
            branches_list.append(f"... 还有 {len(children) - 5} 个分支")
        root_node_branches = "\n".join(branches_list)

        from shared.utils.prompt_loader import StrictPromptLoader

        async with self._db_session_factory() as session:
            base_identity = await StrictPromptLoader.load_and_validate(session, "base_identity_v1", [])
            base_methodology = await StrictPromptLoader.load_and_validate(
                session, "base_methodology_v1", ["stage_desc"]
            )
            react_template = await StrictPromptLoader.load_and_validate(
                session,
                "s1_sop_react_new_v1",
                [
                    "sop_title",
                    "root_node_title",
                    "root_node_type",
                    "root_node_content",
                    "root_node_branches",
                    "known_variables",
                ],
            )
            base_context = await StrictPromptLoader.load_and_validate(session, "base_case_context_v1", ["case_id"])

        formatted_methodology = base_methodology.format(stage_desc=stage_desc)
        formatted_react = react_template.format(
            sop_title=sop_title,
            root_node_title=root_node_title,
            root_node_type=root_node_type,
            root_node_content=root_node_content,
            root_node_branches=root_node_branches,
            known_variables=var_summary,
        )
        formatted_context = base_context.format(case_id=case_id)

        return "\n\n".join([base_identity, formatted_methodology, formatted_react, formatted_context])

    @staticmethod
    def _build_root_node_summary(sop_title: str, root_node: dict) -> str:
        """构建 SOP 根节点摘要（T-AGT-22）。

        格式：
          【根节点：xxx】
          [节点内容摘要]
          【可选分支】
          - n-1-1: xxx
          - n-1-2: xxx

        Args:
            sop_title: SOP 文档标题
            root_node: get_sop_node 返回的根节点字典

        Returns:
            根节点摘要字符串
        """
        parts = [f"【根节点：{root_node.get('title', sop_title)}】"]

        # 节点类型
        node_type = root_node.get("type", "branch")
        parts.append(f"类型：{node_type}")

        # 节点内容（截断，避免过长）
        content = root_node.get("content", "")
        if content:
            truncated_content = content[:500] if len(content) > 500 else content
            parts.append(f"内容摘要：\n{truncated_content}")

        required_variables = root_node.get("required_variables", [])
        if required_variables:
            parts.append("【本节点依赖变量】")
            for variable in required_variables[:8]:
                parts.append(
                    "- {name}: 来源={strategy}, 类型={type}".format(
                        name=variable.get("name", ""),
                        strategy=variable.get("acquisition_strategy", "user_input"),
                        type=variable.get("type", "string"),
                    )
                )

        tool_calls = root_node.get("tool_calls", [])
        if tool_calls:
            parts.append("【建议工具调用】")
            for tool_call in tool_calls[:5]:
                args = tool_call.get("args", {})
                parts.append(f"- {tool_call.get('tool_name')}: {args}")

        # 子节点列表
        children = root_node.get("children", [])
        if children:
            parts.append("【可选分支】")
            for child in children[:5]:  # 最多显示 5 个子节点
                child_id = child.get("node_id", "")
                child_title = child.get("title", "")
                child_line = f"- {child_id}: {child_title}"
                child_required = child.get("required_variables") or []
                if child_required:
                    child_line += "；依赖变量=" + ", ".join(
                        f"{v.get('name')}({v.get('acquisition_strategy', 'user_input')})" for v in child_required[:5]
                    )
                parts.append(child_line)
            if len(children) > 5:
                parts.append(f"... 还有 {len(children) - 5} 个分支")

        return "\n".join(parts)

    # ─── T-AGT-23: SOP 恢复相关 Prompt 构建方法 ───────────────────────────────────

    @staticmethod
    def _build_sop_resume_summary(
        sop_title: str,
        current_node_id: str,
        completed_steps: list[str],
        context_variables: dict,
    ) -> str:
        """构建 SOP 恢复摘要（T-AGT-23）。

        格式（参考 docs/task/agent/events/2026-05-26-SOP执行引擎-M1数据库与M2导航工具化.md）：
          正在执行 SOP：《VM 启动失败排障》
          已完成步骤 3 步，当前位置：存储 I/O 故障 → 磁盘检查
          已知变量：vm_name=prod-vm-001, disk_id=disk-004

        Args:
            sop_title: SOP 文档标题
            current_node_id: 当前节点 ID
            completed_steps: 已完成节点 ID 列表
            context_variables: 运行时变量池

        Returns:
            恢复摘要字符串
        """
        parts = [f"正在执行 SOP：《{sop_title}》"]

        # 已完成步骤数量
        completed_count = len(completed_steps)
        parts.append(f"已完成步骤 {completed_count} 步，当前位置节点：{current_node_id}")

        # 已知变量（提取 value 字段）
        if context_variables:
            var_parts = []
            for var_name, var_info in context_variables.items():
                if isinstance(var_info, dict) and "value" in var_info:
                    var_parts.append(f"{var_name}={var_info['value']}")
                elif isinstance(var_info, str):
                    var_parts.append(f"{var_name}={var_info}")
            if var_parts:
                parts.append(f"已知变量：{', '.join(var_parts[:5])}")  # 最多显示 5 个变量
                if len(var_parts) > 5:
                    parts.append(f"... 还有 {len(var_parts) - 5} 个变量")

        return "\n".join(parts)

    @staticmethod
    def _build_current_node_summary(current_node: dict) -> str:
        """构建当前节点摘要（T-AGT-23，恢复场景使用）。

        格式：
          【当前节点：xxx】
          类型：diagnosis
          内容摘要：...
          【可选分支】
          - n-3-1: xxx

        Args:
            current_node: get_sop_node 返回的当前节点字典

        Returns:
            当前节点摘要字符串
        """
        parts = [f"【当前节点：{current_node.get('title', '未知节点')}】"]

        # 节点类型
        node_type = current_node.get("type", "branch")
        parts.append(f"类型：{node_type}")

        # 节点内容（截断）
        content = current_node.get("content", "")
        if content:
            truncated_content = content[:500] if len(content) > 500 else content
            parts.append(f"内容摘要：\n{truncated_content}")

        required_variables = current_node.get("required_variables", [])
        if required_variables:
            parts.append("【本节点依赖变量】")
            for variable in required_variables[:8]:
                parts.append(
                    "- {name}: 来源={strategy}, 类型={type}".format(
                        name=variable.get("name", ""),
                        strategy=variable.get("acquisition_strategy", "user_input"),
                        type=variable.get("type", "string"),
                    )
                )

        tool_calls = current_node.get("tool_calls", [])
        if tool_calls:
            parts.append("【建议工具调用】")
            for tool_call in tool_calls[:5]:
                args = tool_call.get("args", {})
                parts.append(f"- {tool_call.get('tool_name')}: {args}")

        # 子节点列表（若为 branch 类型）
        children = current_node.get("children", [])
        if children:
            parts.append("【可选分支】")
            for child in children[:5]:
                child_id = child.get("node_id", "")
                child_title = child.get("title", "")
                child_line = f"- {child_id}: {child_title}"
                child_required = child.get("required_variables") or []
                if child_required:
                    child_line += "；依赖变量=" + ", ".join(
                        f"{v.get('name')}({v.get('acquisition_strategy', 'user_input')})" for v in child_required[:5]
                    )
                parts.append(child_line)
            if len(children) > 5:
                parts.append(f"... 还有 {len(children) - 5} 个分支")

        return "\n".join(parts)

    async def _build_sop_resume_prompt(
        self,
        sop_title: str,
        current_node_id: str,
        completed_steps: list[str],
        context_variables: dict,
        current_node: dict,
        diagnostic_stage: str,
        case_id: str,
    ) -> str:
        """构建 SOP 恢复版 System Prompt（数据库化）。"""
        stage_desc_map = {
            "S1": "S1 - 故障定位",
            "S2": "S2 - 假设生成",
            "S3": "S3 - 证据验证",
            "S4": "S4 - 根因确认",
        }
        stage_desc = stage_desc_map.get(diagnostic_stage, diagnostic_stage)

        # 幂等性约束
        completed_nodes_str = ", ".join(completed_steps) if completed_steps else "无"
        completed_steps_count = len(completed_steps)

        # 已知变量
        var_parts = []
        if context_variables:
            for var_name, var_info in context_variables.items():
                if isinstance(var_info, dict) and "value" in var_info:
                    var_parts.append(f"{var_name}={var_info['value']}")
                elif isinstance(var_info, str):
                    var_parts.append(f"{var_name}={var_info}")
        known_variables = ", ".join(var_parts[:5])
        if len(var_parts) > 5:
            known_variables += f" ... 还有 {len(var_parts) - 5} 个变量"

        # 解析当前节点
        current_node_title = current_node.get("title", "未知节点")
        current_node_type = current_node.get("type", "branch")
        current_node_content = current_node.get("content", "")
        if len(current_node_content) > 500:
            current_node_content = current_node_content[:500]

        children = current_node.get("children", [])
        branches_list = []
        for child in children[:5]:
            prereqs = child.get("prerequisites", [])
            prereq_str = f" (前置条件: {', '.join(prereqs)})" if prereqs else ""
            branches_list.append(f"- {child.get('node_id', '')}: {child.get('title', '')}{prereq_str}")
        if len(children) > 5:
            branches_list.append(f"... 还有 {len(children) - 5} 个分支")
        current_node_branches = "\n".join(branches_list)

        from shared.utils.prompt_loader import StrictPromptLoader

        async with self._db_session_factory() as session:
            base_identity = await StrictPromptLoader.load_and_validate(session, "base_identity_v1", [])
            base_methodology = await StrictPromptLoader.load_and_validate(
                session, "base_methodology_v1", ["stage_desc"]
            )
            resume_template = await StrictPromptLoader.load_and_validate(
                session,
                "s2_sop_react_resume_v1",
                [
                    "sop_title",
                    "completed_steps_count",
                    "current_node_id",
                    "known_variables",
                    "current_node_title",
                    "current_node_type",
                    "current_node_content",
                    "current_node_branches",
                    "completed_nodes_str",
                ],
            )
            base_context = await StrictPromptLoader.load_and_validate(session, "base_case_context_v1", ["case_id"])

        formatted_methodology = base_methodology.format(stage_desc=stage_desc)
        formatted_resume = resume_template.format(
            sop_title=sop_title,
            completed_steps_count=completed_steps_count,
            current_node_id=current_node_id,
            known_variables=known_variables,
            current_node_title=current_node_title,
            current_node_type=current_node_type,
            current_node_content=current_node_content,
            current_node_branches=current_node_branches,
            completed_nodes_str=completed_nodes_str,
        )
        formatted_context = base_context.format(case_id=case_id)

        return "\n\n".join([base_identity, formatted_methodology, formatted_resume, formatted_context])

    @staticmethod
    def _truncate_sop_content(sop_content: str, max_chars: int = 8000) -> str:
        """截断超长 SOP 内容（降级路径使用）。"""
        if len(sop_content) > max_chars:
            sop_content = sop_content[:max_chars]
            sop_content += "\n\n[注意：SOP 文档已截断，请基于已有信息分步推理。必要时通过工具获取更多细节]"
        return sop_content

    @staticmethod
    def _build_sop_prompt(
        sop_content: str,
        sop_title: str,
        diagnostic_stage: str,
        case_id: str,
    ) -> str:
        """单元测试兼容性静态方法，不查询数据库，直接格式化默认模板（专门为测试服务）"""
        stage_desc_map = {
            "S1": "S1 - 故障定位",
            "S2": "S2 - 假设生成",
            "S3": "S3 - 证据验证",
            "S4": "S4 - 根因确认",
        }
        stage_desc = stage_desc_map.get(diagnostic_stage, diagnostic_stage)
        truncated_content = InvestigationAgent._truncate_sop_content(sop_content)

        base_identity = "你是深信服超融合基础设施（HCI）智能排障专家助手。\n你拥有完整的 HCI 平台工作原理知识：虚拟机生命周期、分布式存储、vxlan网络、\nIPMI硬件管理、acli诊断工具集的完整用法。\n你的目标是协助现场工程师快速定位 and 解决 HCI 平台故障。"
        base_methodology = "【工作方法论】\n当前诊断阶段：{stage_desc}\n\n标准诊断流程：\nS0 意图识别：从客户描述提取关键实体（虚拟机名/集群/时间点），同时查看告警日志和操作日志，确认客户真实问题\nS1 故障定位：向客户提出 1-3 个精准确认问题，定位到最小故障分类\nS2 假设生成：列出 2-3 个最可能的根因假设，按概率排序\nS3 验证执行：逐一执行诊断命令，收集系统状态证据\nS4 根因确认：根据证据确定根因\nS5 方案输出：提供明确可执行的修复步骤\nS6 验证闭环：确认问题已解决，记录知识"
        legacy_template = "【知识使用规范】\n你有 SOP 排障流程可用，请严格按其步骤顺序执行，在每个判断节点收集证据后再做决策。\n\n【SOP 排障流程 | 来源：{sop_title}】\n{sop_content}"
        base_context = "---\n当前工单 ID：{case_id}"

        formatted_methodology = base_methodology.format(stage_desc=stage_desc)
        formatted_legacy = legacy_template.format(
            sop_title=sop_title,
            sop_content=truncated_content,
        )
        formatted_context = base_context.format(case_id=case_id)

        return "\n\n".join([base_identity, formatted_methodology, formatted_legacy, formatted_context])

    async def _build_sop_prompt_legacy(
        self,
        sop_content: str,
        sop_title: str,
        diagnostic_stage: str,
        case_id: str,
    ) -> str:
        """构建 SOP 模式 System Prompt（数据库化，降级文本路径）。"""
        stage_desc_map = {
            "S1": "S1 - 故障定位",
            "S2": "S2 - 假设生成",
            "S3": "S3 - 证据验证",
            "S4": "S4 - 根因确认",
        }
        stage_desc = stage_desc_map.get(diagnostic_stage, diagnostic_stage)
        truncated_content = self._truncate_sop_content(sop_content)

        from shared.utils.prompt_loader import StrictPromptLoader

        async with self._db_session_factory() as session:
            base_identity = await StrictPromptLoader.load_and_validate(session, "base_identity_v1", [])
            base_methodology = await StrictPromptLoader.load_and_validate(
                session, "base_methodology_v1", ["stage_desc"]
            )
            legacy_template = await StrictPromptLoader.load_and_validate(
                session, "s3_sop_legacy_v1", ["sop_title", "sop_content"]
            )
            base_context = await StrictPromptLoader.load_and_validate(session, "base_case_context_v1", ["case_id"])

        formatted_methodology = base_methodology.format(stage_desc=stage_desc)
        formatted_legacy = legacy_template.format(
            sop_title=sop_title,
            sop_content=truncated_content,
        )
        formatted_context = base_context.format(case_id=case_id)

        return "\n\n".join([base_identity, formatted_methodology, formatted_legacy, formatted_context])

    async def _build_fallback_prompt(
        self,
        category_id: str,
        diagnostic_stage: str,
        case_id: str,
    ) -> str:
        """构建机制推理降级 System Prompt（数据库化）。"""
        stage_desc_map = {
            "S1": "S1 - 故障定位",
            "S2": "S2 - 假设生成",
            "S3": "S3 - 证据验证",
            "S4": "S4 - 根因确认",
        }
        stage_desc = stage_desc_map.get(diagnostic_stage, diagnostic_stage)

        from shared.utils.prompt_loader import StrictPromptLoader

        async with self._db_session_factory() as session:
            base_identity = await StrictPromptLoader.load_and_validate(session, "base_identity_v1", [])
            base_methodology = await StrictPromptLoader.load_and_validate(
                session, "base_methodology_v1", ["stage_desc"]
            )
            fallback_template = await StrictPromptLoader.load_and_validate(session, "s4_fallback_v1", ["category_id"])
            base_context = await StrictPromptLoader.load_and_validate(session, "base_case_context_v1", ["case_id"])

        formatted_methodology = base_methodology.format(stage_desc=stage_desc)
        formatted_fallback = fallback_template.format(category_id=category_id)
        formatted_context = base_context.format(case_id=case_id)

        return "\n\n".join([base_identity, formatted_methodology, formatted_fallback, formatted_context])

    # ─── 工具方法（内部）──────────────────────────────────────────────────────

    @staticmethod
    def _extract_user_query(messages: list[dict]) -> str:
        """从消息列表中提取最后一条用户消息内容。"""
        for msg in reversed(messages):
            if msg.get("role") == "user":
                content = msg.get("content", "")
                return content[:500] if isinstance(content, str) else ""
        return ""

    @staticmethod
    def _split_text_chunks(text: str, chunk_size: int = 100) -> list[str]:
        """将长文本分割为固定大小的 chunk 列表（用于流式输出模拟）。"""
        return [text[i : i + chunk_size] for i in range(0, len(text), chunk_size)]
