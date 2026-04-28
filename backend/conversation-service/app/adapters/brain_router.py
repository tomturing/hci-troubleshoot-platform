"""
BrainRouter：大脑路由器（T1-6）

根据 assistant_type 路由到对应的 BrainPort 实现，
并在目标大脑不可达时自动降级到 htp 原有大脑。

设计说明：
- BrainRouter 是 ConversationService 的成员，不是独立微服务
- 路由逻辑：assistant_type=ops-agent → OpsAgentBrainAdapter
             其他（openclaw/glm/未知）→ HTPBrainAdapter
- 降级逻辑：OpsAgentBrainAdapter raise BrainUnavailableError 时
            自动切换到 HTPBrainAdapter 并记录降级事件
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from typing import Any

from app.core.brain_port import BrainEvent, BrainTextChunk, BrainUnavailableError
from app.adapters.htp_brain_adapter import HTPBrainAdapter
from app.adapters.ops_agent_brain_adapter import OpsAgentBrainAdapter

logger = logging.getLogger("brain-router")

# ops-agent 大脑路由的 assistant_type 标识
OPS_AGENT_TYPE = "ops-agent"

# 降级提示消息（用户可见）
_FALLBACK_NOTICE = "\n\n> [系统提示] ops-agent 暂时不可用，已自动切换到备用助手继续为您服务。\n\n"


class BrainRouter:
    """大脑路由器：根据 assistant_type 将请求路由到对应的大脑实现。

    注入关系：
        ConversationService.__init__() 接收 BrainRouter，
        send_message_stream_only() 委托给 brain_router.process()。

    BrainRouter 知道所有大脑，但 ConversationService 只知道 BrainRouter。
    """

    def __init__(
        self,
        htp_adapter: HTPBrainAdapter,
        ops_agent_adapter: OpsAgentBrainAdapter | None = None,
    ) -> None:
        self._htp = htp_adapter
        self._ops_agent = ops_agent_adapter

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
    ) -> AsyncGenerator[BrainEvent, None]:
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
            BrainEvent 序列（来自目标大脑或降级后的备用大脑）。
        """
        if assistant_type == OPS_AGENT_TYPE and self._ops_agent is not None:
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
    ) -> AsyncGenerator[BrainEvent, None]:
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
        except BrainUnavailableError as exc:
            logger.warning(
                "BrainRouter: ops-agent 不可达，降级到 htp 大脑. session_id=%s reason=%s",
                session_id,
                exc.reason,
            )
            # 向用户发送降级通知
            yield BrainTextChunk(content=_FALLBACK_NOTICE)
            # 降级到 htp 大脑（用 "openclaw" 兜底）
            async for event in self._htp.process(
                session_id=session_id,
                messages=messages,
                env_context=None,  # htp 大脑环境上下文已在 system_prompt 中
                stream=stream,
                assistant_type="openclaw",
                case_id=case_id,
                user_id=user_id,
            ):
                yield event
