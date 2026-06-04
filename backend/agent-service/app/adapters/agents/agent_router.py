"""
AgentRouter：大脑路由器（v4.3）

路由逻辑：
  1. assistant_type=ops-agent → OpsAgentAdapter
  2. assistant_type=pai-agent → PaiAgentAdapter
  3. assistant_type=htp-agent / 其他：
     - stage=S0          → TriageAgent（意图识别）
     - stage=S1/S2/S3/S4 → InvestigationAgent（诊断调查）【T-AGT-11】
     - stage=S5          → RemediationAgent（修复执行）

设计说明：
  - AgentRouter 是 ConversationService 的成员，不是独立微服务
  - 降级逻辑：
    - ops-agent 未启用时降级到 InvestigationAgent
    - pai-agent 不可达时降级到 InvestigationAgent
  - v4.3：移除 DiagnosticAgent（僵尸组件，从未被调用）
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any

from shared.clients import AIAssistantRegistry
from shared.observability.logger import get_logger

from app.adapters.agents.htp.investigation_agent import InvestigationAgent  # T-AGT-11：S1-S4
from app.adapters.agents.htp.kbd_model import KBD
from app.adapters.agents.htp.remediation_agent import RemediationAgent
from app.adapters.agents.htp.triage_agent import TriageAgent  # T-AGT-10：S0
from app.adapters.agents.ops.ops_agent_adapter import OpsAgentAdapter
from app.domain.agent_port import AgentEvent, AgentTextChunk, AgentUnavailableError

if TYPE_CHECKING:
    from app.adapters.agents.pai.pai_agent_adapter import PaiAgentAdapter

from app.config import settings

logger = get_logger("agent-router")

# 各大脑路由的 assistant_type 标识
OPS_AGENT_TYPE = "ops-agent"
PYDANTIC_AI_TYPE = "pai-agent"

# 降级提示消息（用户可见）
_FALLBACK_NOTICE = "\n\n> [系统提示] ops-agent 暂时不可用，已自动切换到备用助手继续为您服务。\n\n"
_OPS_AGENT_DISABLED_NOTICE = "\n\n> [系统提示] ops-agent 服务未启用，已自动切换到备用助手继续为您服务。\n\n"

# 阶段分组常量
_INTENT_STAGES = {"S0"}
_DIAGNOSTIC_STAGES = {"S1", "S2", "S3", "S4"}
_REMEDIATION_STAGES = {"S5"}


class AgentRouter:
    """大脑路由器：按 stage + assistant_type 将请求路由到对应的大脑实现。

    注入关系：
        ConversationService.__init__() 接收 AgentRouter，
        send_message_stream_only() 委托给 agent_router.process()。

    AgentRouter 知道所有大脑，但 ConversationService 只知道 AgentRouter。
    """

    def __init__(
        self,
        triage_agent: TriageAgent,  # T-AGT-10：S0 意图识别
        investigation_agent: InvestigationAgent,  # T-AGT-11：S1-S4 诊断调查
        remediation_agent: RemediationAgent | None = None,
        ops_agent_adapter: OpsAgentAdapter | None = None,
        pai_adapter: PaiAgentAdapter | None = None,
        ai_registry: AIAssistantRegistry | None = None,
    ) -> None:
        self._triage_agent = triage_agent
        self._investigation_agent = investigation_agent
        self._remediation_agent = remediation_agent
        self._ops_agent = ops_agent_adapter
        self._pai = pai_adapter
        self._ai_registry = ai_registry

    def get_ops_agent_adapter(self) -> OpsAgentAdapter | None:
        """返回 OpsAgentAdapter 实例（供 interactive-response 提交使用）。"""
        return self._ops_agent

    async def process(
        self,
        *,
        assistant_type: str,
        session_id: str,
        messages: list[dict[str, Any]],
        env_context: dict[str, Any] | None = None,
        stream: bool = True,
        case_id: str = "",
        user_id: str = "",
        diagnostic_stage: str = "S0",
        category_id: str | None = None,
        matched_kbds: list[KBD] | None = None,
        root_cause: str = "",
        solution: str = "",
        execution_mode: str = "direct",  # 保留兼容旧调用方
        system_prompt: str | None = None,  # 保留兼容旧调用方
        sop_resume_context: dict[str, Any] | None = None,  # T-AGT-23: SOP 执行恢复上下文
    ) -> AsyncGenerator[AgentEvent, None]:
        """路由大脑请求，按 stage + assistant_type 分流。

        Args:
            assistant_type: 助手类型标识（路由依据）。
            session_id: 对话 session ID。
            messages: OpenAI 格式消息列表。
            env_context: HCI 实时环境上下文。
            stream: 是否流式输出。
            case_id: 工单 ID。
            user_id: 用户 ID。
            diagnostic_stage: 当前诊断阶段（S0/S1/S2/S3/S4/S5/S6）。
            category_id: S0 确认的分类编码（S1+ 阶段必须）。
            matched_kbds: S4 确认的匹配 KBD（S5 阶段使用）。
            root_cause: 已确认根因（S5 使用）。
            solution: 推荐方案（S5 使用）。
            execution_mode: 兼容旧调用路径，不影响新路由逻辑。
            system_prompt: 自定义 system_prompt（可选，保留兼容）。
            sop_resume_context: SOP 执行恢复上下文（T-AGT-23，用于断线重连恢复）。

        Yields:
            AgentEvent 序列（来自目标大脑或降级后的备用大脑）。
        """
        # 1. ops-agent 路由（独立路径）
        if assistant_type == OPS_AGENT_TYPE:
            if self._ops_agent is None:
                logger.warning(
                    event="ops_agent_disabled",
                    message="ops-agent 未启用，降级到 InvestigationAgent",
                    session_id=session_id,
                )
                yield AgentTextChunk(content=_OPS_AGENT_DISABLED_NOTICE)
                async for event in self._investigation_agent.process(
                    session_id=session_id,
                    messages=messages,
                    category_id=category_id or "",
                    diagnostic_stage=diagnostic_stage,
                    env_context=env_context,
                    assistant_type=settings.OPS_AGENT_FALLBACK_ASSISTANT_TYPE,
                    case_id=case_id,
                    user_id=user_id,
                    sop_resume_context=sop_resume_context,  # T-AGT-23: SOP 执行恢复上下文
                ):
                    yield event
            else:
                try:
                    async for event in self._ops_agent.process(
                        session_id=session_id,
                        messages=messages,
                        env_context=env_context,
                        stream=stream,
                        user_id=user_id,
                    ):
                        yield event
                except AgentUnavailableError as exc:
                    logger.warning(
                        event="ops_agent_unavailable",
                        message=f"ops-agent 不可达，降级到 InvestigationAgent: {exc.reason}",
                        session_id=session_id,
                    )
                    yield AgentTextChunk(content=_FALLBACK_NOTICE)
                    async for event in self._investigation_agent.process(
                        session_id=session_id,
                        messages=messages,
                        category_id=category_id or "",
                        diagnostic_stage=diagnostic_stage,
                        env_context=env_context,
                        assistant_type=settings.OPS_AGENT_FALLBACK_ASSISTANT_TYPE,
                        case_id=case_id,
                        user_id=user_id,
                        sop_resume_context=sop_resume_context,  # T-AGT-23: SOP 执行恢复上下文
                    ):
                        yield event
            return

        # 2. pai-agent 路由（独立路径）
        if assistant_type == PYDANTIC_AI_TYPE:
            if self._pai is None:
                logger.warning(
                    event="pai_agent_disabled",
                    message="pai-agent 未启用，降级到 InvestigationAgent",
                    session_id=session_id,
                )
                yield AgentTextChunk(content=_FALLBACK_NOTICE)
                fallback_type = (
                    self._ai_registry.get_default_type()
                    if self._ai_registry is not None
                    else settings.OPS_AGENT_FALLBACK_ASSISTANT_TYPE
                )
                async for event in self._investigation_agent.process(
                    session_id=session_id,
                    messages=messages,
                    category_id=category_id or "",
                    diagnostic_stage=diagnostic_stage,
                    env_context=env_context,
                    assistant_type=fallback_type,
                    case_id=case_id,
                    user_id=user_id,
                    sop_resume_context=sop_resume_context,  # T-AGT-23: SOP 执行恢复上下文
                ):
                    yield event
            else:
                try:
                    async for event in self._pai.process(
                        session_id=session_id,
                        messages=messages,
                        env_context=env_context,
                        stream=stream,
                        category_id=category_id,  # 传递 category_id 给 pai-agent
                    ):
                        yield event
                except AgentUnavailableError as exc:
                    logger.warning(
                        event="pai_agent_unavailable",
                        message=f"pai-agent 不可达，降级到 InvestigationAgent: {exc.reason}",
                        session_id=session_id,
                    )
                    yield AgentTextChunk(content=_FALLBACK_NOTICE)
                    async for event in self._investigation_agent.process(
                        session_id=session_id,
                        messages=messages,
                        category_id=category_id or "",
                        diagnostic_stage=diagnostic_stage,
                        env_context=env_context,
                        assistant_type=settings.OPS_AGENT_FALLBACK_ASSISTANT_TYPE,
                        case_id=case_id,
                        user_id=user_id,
                        sop_resume_context=sop_resume_context,  # T-AGT-23: SOP 执行恢复上下文
                    ):
                        yield event
            return

        # 3. HTP Agent 路由（按 stage 分流）
        if diagnostic_stage in _INTENT_STAGES:
            # S0：意图识别（T-AGT-10：使用 TriageAgent）
            logger.info(
                event="route_triage_agent",
                message=f"路由到 TriageAgent: stage=S0, assistant_type={assistant_type}",
                session_id=session_id,
            )
            async for event in self._triage_agent.process(
                session_id=session_id,
                messages=messages,
                env_context=env_context,
                assistant_type=assistant_type,
                case_id=case_id,
                user_id=user_id,
            ):
                yield event

        elif diagnostic_stage in _REMEDIATION_STAGES:
            # S5：修复执行
            if self._remediation_agent is None:
                logger.warning(
                    event="route_remediation_missing",
                    message="RemediationAgent 未注入，无法执行修复操作",
                    session_id=session_id,
                )
                yield AgentTextChunk(content="[提示] 修复执行模块暂不可用，请根据诊断报告手动操作。")
                return

            if not category_id:
                yield AgentTextChunk(content="[错误] S5 阶段需要 category_id，请先完成诊断")
                return

            logger.info(
                event="route_remediation_agent",
                message=f"路由到 RemediationAgent: stage=S5, category_id={category_id}",
                session_id=session_id,
            )
            async for event in self._remediation_agent.process(
                session_id=session_id,
                messages=messages,
                matched_kbds=matched_kbds,
                root_cause=root_cause,
                solution=solution,
                assistant_type=assistant_type,
                case_id=case_id,
                user_id=user_id,
            ):
                yield event

        else:
            # S1-S4：诊断调查（默认路径）【T-AGT-11：使用 InvestigationAgent】
            if not category_id:
                logger.warning(
                    event="route_investigation_missing_category",
                    message="S1+ 阶段缺少 category_id",
                    session_id=session_id,
                    stage=diagnostic_stage,
                )
                yield AgentTextChunk(content="[错误] S1+ 阶段需要 category_id，请先完成意图识别")
                return

            logger.info(
                event="route_investigation_agent",
                message=f"路由到 InvestigationAgent: stage={diagnostic_stage}, category_id={category_id}",
                session_id=session_id,
                sop_resume=sop_resume_context is not None,
            )
            async for event in self._investigation_agent.process(
                session_id=session_id,
                messages=messages,
                category_id=category_id,
                diagnostic_stage=diagnostic_stage,
                env_context=env_context,
                assistant_type=assistant_type,
                case_id=case_id,
                user_id=user_id,
                sop_resume_context=sop_resume_context,  # T-AGT-23: SOP 执行恢复上下文
            ):
                yield event
