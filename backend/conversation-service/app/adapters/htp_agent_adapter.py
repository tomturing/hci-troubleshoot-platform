"""
HTPAgentAdapter：htp 原有大脑的 AgentPort 实现（T1-4：逻辑搬家）

将 ConversationService.send_message_stream_only() 中的"大脑执行"部分
封装为独立 Adapter，实现 AgentPort Protocol。

改动原则：
- 仅搬家，不修改任何业务逻辑
- ConversationService 保留会话管理（消息保存、阶段转换、诊断状态机），
  将大脑调用委托给 HTPAgentAdapter.process()
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncGenerator
from typing import Any

from shared.observability.metrics import AI_REQUESTS_TOTAL, AI_TTFT_SECONDS
from shared.utils.exceptions import AIStreamError

from app.core.agent_port import AgentEvent, AgentTextChunk, AgentUnavailableError
from app.services.ai_client import AIAssistantRegistry
from app.services.scheduler_client import SchedulerClient

logger = logging.getLogger("htp-brain-adapter")


class HTPAgentAdapter:
    """htp 原有 S0-S6 大脑的 AgentPort 适配器。

    封装：
    - AIAssistantRegistry 客户端查找
    - SchedulerClient pod 端点分配
    - httpx 流式调用
    - TTFT / Prometheus metrics 采集

    不负责：消息持久化、阶段转换检测（仍由 ConversationService 负责）。
    """

    def __init__(
        self,
        ai_registry: AIAssistantRegistry,
        scheduler_client: SchedulerClient | None = None,
    ) -> None:
        self._ai_registry = ai_registry
        self._scheduler_client = scheduler_client

    async def process(
        self,
        *,
        session_id: str,
        messages: list[dict[str, Any]],
        env_context: dict[str, Any] | None = None,
        stream: bool = True,
        # htp 侧额外参数（AgentPort 扩展，通过 **kwargs 传入不影响 Protocol 兼容性）
        assistant_type: str = "openclaw",
        case_id: str = "",
        user_id: str = "",
    ) -> AsyncGenerator[AgentEvent, None]:
        """调用 htp 原有大脑，以流式 AgentTextChunk 产出响应。

        Args:
            session_id: 对话 session ID（htp 大脑用于日志关联，不做跨轮次恢复）。
            messages: OpenAI 格式的消息列表（含 system prompt + 历史 + 当前用户消息）。
            env_context: htp 侧已经在 system_prompt 中注入了环境上下文，此参数此处忽略。
            stream: 是否流式输出（当前始终 True，htp 大脑只支持流式）。
            assistant_type: htp 注册表中的助手类型标识。
            case_id: 工单 ID，用于 scheduler 分配 pod 和 metrics 标签。
            user_id: 用户 ID，用于 AI 调用的 user 字段。
        """
        ai_client = self._ai_registry.get_client(assistant_type)
        if not ai_client:
            raise AgentUnavailableError(
                agent_name="htp",
                reason=f"未找到助手类型 '{assistant_type}' 的客户端",
            )

        pod_endpoint = await self._resolve_pod_endpoint(case_id, assistant_type)

        stream_start = time.monotonic()
        ttft_logged = False

        try:
            async for chunk in ai_client.chat_completion_stream(
                messages=messages,
                user_id=user_id or f"case-{case_id}",
                pod_endpoint=pod_endpoint,
            ):
                if chunk:
                    if not ttft_logged:
                        ttft_ms = int((time.monotonic() - stream_start) * 1000)
                        logger.info(
                            "htp brain TTFT: %dms assistant_type=%s case_id=%s",
                            ttft_ms,
                            assistant_type,
                            case_id,
                        )
                        AI_TTFT_SECONDS.labels(assistant_type=assistant_type).observe(ttft_ms / 1000.0)
                        ttft_logged = True
                    yield AgentTextChunk(content=chunk)

            AI_REQUESTS_TOTAL.labels(assistant_type=assistant_type, status="success").inc()

        except asyncio.CancelledError:
            logger.info("HTPAgentAdapter: stream cancelled session_id=%s", session_id)
            raise
        except AIStreamError as exc:
            AI_REQUESTS_TOTAL.labels(assistant_type=assistant_type, status="error").inc()
            logger.error(
                "HTPAgentAdapter: AIStreamError %s session_id=%s", exc, session_id
            )
            raise AgentUnavailableError(agent_name="htp", reason=str(exc)) from exc
        except Exception as exc:
            AI_REQUESTS_TOTAL.labels(assistant_type=assistant_type, status="error").inc()
            logger.error(
                "HTPAgentAdapter: unexpected error %s session_id=%s",
                type(exc).__name__,
                session_id,
            )
            raise AgentUnavailableError(agent_name="htp", reason=str(exc)) from exc

    async def _resolve_pod_endpoint(self, case_id: str, assistant_type: str) -> str | None:
        """通过 scheduler 分配 pod 端点（从 ConversationService 搬家）。"""
        if not self._scheduler_client or not case_id:
            return None
        try:
            endpoint = await self._scheduler_client.allocate_pod(
                case_id=case_id, assistant_type=assistant_type
            )
            return endpoint
        except Exception as exc:
            logger.warning(
                "HTPAgentAdapter: scheduler allocate_pod failed, using fallback. error=%s", exc
            )
            return self._get_fallback_endpoint(assistant_type)

    def _get_fallback_endpoint(self, assistant_type: str) -> str | None:
        """scheduler 不可用时返回静态兜底端点。"""
        from app.config import settings

        fallback_map: dict[str, str | None] = {
            "openclaw": getattr(settings, "OPENCLAW_BASE_URL", None),
        }
        return fallback_map.get(assistant_type)
