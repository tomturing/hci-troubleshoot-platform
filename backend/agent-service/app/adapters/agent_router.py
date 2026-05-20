"""
AgentRouter：大脑路由器（T1-6）

根据 assistant_type 路由到对应的 AgentPort 实现，
并在目标大脑不可达时自动降级到 htp 原有大脑。

设计说明：
- AgentRouter 是 ConversationService 的成员，不是独立微服务
- 路由逻辑：assistant_type=ops-agent → OpsAgentAdapter
             其他（openclaw/glm/未知）→ HTPAgentAdapter
- 降级逻辑：
  - ops-agent 未启用（_ops_agent is None）时降级到 htp
  - OpsAgentAdapter raise AgentUnavailableError 时降级到 htp
  - 两种场景统一使用 _fallback_to_htp() 方法处理
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any

from shared.clients import AIAssistantRegistry

from app.adapters.htp_agent_adapter import HTPAgentAdapter
from app.adapters.ops_agent_adapter import OpsAgentAdapter
from app.core.agent_port import AgentEvent, AgentTextChunk, AgentUnavailableError

if TYPE_CHECKING:
    from app.adapters.pai_agent_adapter import PaiAgentAdapter

logger = logging.getLogger("brain-router")

# 各大脑路由的 assistant_type 标识
OPS_AGENT_TYPE = "ops-agent"
PYDANTIC_AI_TYPE = "pydantic-ai"

# 降级提示消息（用户可见）
_FALLBACK_NOTICE = "\n\n> [系统提示] ops-agent 暂时不可用，已自动切换到备用助手继续为您服务。\n\n"

# ops-agent 未启用提示（用户可见）
_OPS_AGENT_DISABLED_NOTICE = "\n\n> [系统提示] ops-agent 服务未启用，已自动切换到备用助手继续为您服务。\n\n"


class AgentRouter:
    """大脑路由器：根据 assistant_type 将请求路由到对应的大脑实现。

    注入关系：
        ConversationService.__init__() 接收 AgentRouter，
        send_message_stream_only() 委托给 agent_router.process()。

    AgentRouter 知道所有大脑，但 ConversationService 只知道 AgentRouter。
    """

    def __init__(
        self,
        htp_adapter: HTPAgentAdapter,
        ops_agent_adapter: OpsAgentAdapter | None = None,
        pai_adapter: PaiAgentAdapter | None = None,
        ai_registry: AIAssistantRegistry | None = None,
    ) -> None:
        self._htp = htp_adapter
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
    ) -> AsyncGenerator[AgentEvent, None]:
        """路由大脑请求，OpsAgent 不可用时自动降级。

        Args:
            assistant_type: 助手类型标识（路由依据）。
            session_id: 对话 session ID。
            messages: OpenAI 格式消息列表。
            env_context: HCI 实时环境上下文（ops-agent 侧注入）。
            stream: 是否流式输出。
            case_id: 工单 ID（htp 大脑需要）。
            user_id: 用户 ID。

        Yields:
            AgentEvent 序列（来自目标大脑或降级后的备用大脑）。
        """
        if assistant_type == PYDANTIC_AI_TYPE:
            if self._pai is None:
                logger.warning(
                    "AgentRouter: pydantic-ai 未启用，降级到 htp 大脑. session_id=%s",
                    session_id,
                )
                async for event in self._htp.process(
                    session_id=session_id,
                    messages=messages,
                    env_context=env_context,
                    stream=stream,
                    assistant_type="glm-4-flash",
                    case_id=case_id,
                    user_id=user_id,
                ):
                    yield event
            else:
                try:
                    async for event in self._pai.process(
                        session_id=session_id,
                        messages=messages,
                        env_context=env_context,
                        stream=stream,
                    ):
                        yield event
                except AgentUnavailableError as exc:
                    logger.warning(
                        "AgentRouter: pydantic-ai 不可达，降级到 htp 大脑. session_id=%s reason=%s",
                        session_id,
                        exc.reason,
                    )
                    async for event in self._fallback_to_htp(
                        notice=_FALLBACK_NOTICE,
                        session_id=session_id,
                        messages=messages,
                        stream=stream,
                        case_id=case_id,
                        user_id=user_id,
                    ):
                        yield event
        elif assistant_type == OPS_AGENT_TYPE:
            if self._ops_agent is None:
                # ops-agent 未启用，降级到 htp 大脑
                logger.warning(
                    "AgentRouter: ops-agent 未启用，降级到 htp 大脑. session_id=%s",
                    session_id,
                )
                async for event in self._fallback_to_htp(
                    notice=_OPS_AGENT_DISABLED_NOTICE,
                    session_id=session_id,
                    messages=messages,
                    stream=stream,
                    case_id=case_id,
                    user_id=user_id,
                ):
                    yield event
            else:
                async for event in self._route_ops_agent(
                    session_id=session_id,
                    messages=messages,
                    env_context=env_context,
                    stream=stream,
                    user_id=user_id,
                    case_id=case_id,
                ):
                    yield event
        else:
            async for event in self._htp.process(
                session_id=session_id,
                messages=messages,
                env_context=env_context,
                stream=stream,
                assistant_type=assistant_type,
                case_id=case_id,
                user_id=user_id,
            ):
                yield event

    async def _route_ops_agent(
        self,
        *,
        session_id: str,
        messages: list[dict[str, Any]],
        env_context: dict[str, Any] | None,
        stream: bool,
        user_id: str,
        case_id: str,
    ) -> AsyncGenerator[AgentEvent, None]:
        """尝试 ops-agent 大脑，失败时降级到 htp 大脑。"""
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
                "AgentRouter: ops-agent 不可达，降级到 htp 大脑. session_id=%s reason=%s",
                session_id,
                exc.reason,
            )
            async for event in self._fallback_to_htp(
                notice=_FALLBACK_NOTICE,
                session_id=session_id,
                messages=messages,
                stream=stream,
                case_id=case_id,
                user_id=user_id,
            ):
                yield event

    async def _fallback_to_htp(
        self,
        *,
        notice: str,
        session_id: str,
        messages: list[dict[str, Any]],
        stream: bool,
        case_id: str,
        user_id: str,
    ) -> AsyncGenerator[AgentEvent, None]:
        """统一的降级处理：发送提示消息并切换到 htp 大脑。

        Args:
            notice: 用户可见的降级提示文本。
            session_id: 对话 session ID。
            messages: OpenAI 格式消息列表。
            stream: 是否流式输出。
            case_id: 工单 ID。
            user_id: 用户 ID。

        Yields:
            AgentEvent 序列（降级提示 + htp 大脑输出）。
        """
        # 向用户发送降级通知
        yield AgentTextChunk(content=notice)
        # 降级助手类型选取优先级：
        # 1. 环境变量 OPS_AGENT_FALLBACK_ASSISTANT_TYPE 显式指定
        # 2. ai_registry.get_default_type()：读 ConfigMap 中 is_default=true 的助手，或第一个已注册助手
        # 3. 无 ai_registry 注入时，使用固定 assistant_type 作为最终兜底
        env_fallback = os.environ.get("OPS_AGENT_FALLBACK_ASSISTANT_TYPE", "")
        if env_fallback:
            fallback_type = env_fallback
        elif self._ai_registry is not None:
            fallback_type = self._ai_registry.get_default_type()
        else:
            fallback_type = "glm-4.7"  # 无 ai_registry 注入时的最终兜底 assistant_type
        logger.info(
            "AgentRouter: 降级助手类型=%s session_id=%s",
            fallback_type,
            session_id,
        )
        async for event in self._htp.process(
            session_id=session_id,
            messages=messages,
            env_context=None,  # htp 大脑环境上下文已在 system_prompt 中
            stream=stream,
            assistant_type=fallback_type,
            case_id=case_id,
            user_id=user_id,
        ):
            yield event
