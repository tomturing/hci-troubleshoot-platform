"""
OpsAgentBrainAdapter：ops-agent ACP 客户端适配器（方案E实现）

将 ops-agent 从"无交互单向文本生成器"升级为"完整双向交互引擎"。
核心改进（对比原 /v1/chat/completions 方案）：
  - 使用 ACP REST 协议：session_new → submit_prompt → SSE events → submit_response
  - ACP session ID 直接使用 htp conversation_id（利用 ops-agent T-E1 可选参数），
    无需维护 ID 映射表，session 在 ops-agent 跨轮次持久化
  - 支持 BrainInteractiveRequest 事件（_ops/request_input → 前端 SOP 操作卡）
  - ops-agent 不可达时 raise BrainUnavailableError（降级机制与原方案一致）

跨仓库接口依据：docs/solution/ai-assistant/events/2026-05-08-ops-agent方案E-ACP-REST接口设计与实现.md

第一性原理分析（改动必要性）：
  - process() 接口签名不变（BrainPort 兼容），BrainRouter 和 ConversationService 无需改动
  - 内部切换 HTTP 端点（/v1/chat/completions → /acp/sessions/*），调用方完全无感知
  - BrainUnavailableError 降级路径与原方案完全一致
  - BrainInteractiveRequest 若无处理则静默丢弃（不影响文本响应功能，支持渐进式实现）
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import AsyncGenerator
from typing import Any

import httpx

from app.core.brain_port import (
    BrainEvent,
    BrainInteractiveRequest,
    BrainStageUpdate,
    BrainTextChunk,
    BrainUnavailableError,
)

logger = logging.getLogger("ops-agent-brain-adapter")


class OpsAgentBrainAdapter:
    """ops-agent ACP 客户端适配器。

    通过 ACP REST 协议调用 ops-agent，实现完整的双向交互会话。
    ACP session ID 直接复用 htp conversation_id，ops-agent 侧跨轮次持久化会话上下文。

    接口契约：方案E设计文档
    """

    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        default_model: str = "ops-agent",
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key or os.environ.get("OPS_AGENT_API_KEY")
        self._default_model = default_model
        _read_timeout = float(os.environ.get("OPS_AGENT_READ_TIMEOUT_SEC", "300.0"))
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=_read_timeout, write=10.0, pool=10.0)
        )

    async def process(
        self,
        *,
        session_id: str,
        messages: list[dict[str, Any]],
        env_context: dict[str, Any] | None = None,
        stream: bool = True,
        user_id: str = "",
        **_kwargs: Any,
    ) -> AsyncGenerator[BrainEvent, None]:
        """调用 ops-agent ACP REST API，以流式 BrainEvent 产出响应。

        session_id 直接作为 ACP session ID 传入 ops-agent，
        ops-agent 侧在跨轮次对话中保持会话上下文（无需在此处维护映射表）。

        Yields:
            BrainTextChunk: Agent 输出的文本片段
            BrainStageUpdate: 会话标题更新（session_info_update）
            BrainInteractiveRequest: Agent 请求用户交互（SOP操作卡/提问）

        Raises:
            BrainUnavailableError: ops-agent 服务不可达或返回非 2xx 状态码。
        """
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        logger.info(
            "OpsAgentBrainAdapter: 开始 ACP 会话 session_id=%s has_env=%s messages=%d",
            session_id,
            env_context is not None,
            len(messages),
        )

        try:
            # Step 1：幂等创建 ACP 会话（session_id = htp conversation_id）
            await self._ensure_acp_session(session_id, headers)

            # Step 2：提取当前轮次的用户消息，构造 ACP prompt
            user_text = self._extract_last_user_message(messages)
            if env_context:
                user_text = self._inject_env_context(user_text, env_context)

            prompt = [{"type": "text", "text": user_text}]

            # Step 3：提交 prompt（非阻塞，立即返回 202）
            await self._submit_prompt(session_id, prompt, headers)

            # Step 4：消费 SSE 事件流，翻译为 BrainEvent
            stream_start = time.monotonic()
            ttft_logged = False
            async for brain_event in self._consume_events(session_id, headers):
                if isinstance(brain_event, BrainTextChunk) and not ttft_logged:
                    ttft_ms = int((time.monotonic() - stream_start) * 1000)
                    logger.info(
                        "OpsAgentBrainAdapter TTFT: %dms session_id=%s", ttft_ms, session_id
                    )
                    ttft_logged = True
                yield brain_event

        except BrainUnavailableError:
            raise
        except httpx.TimeoutException as exc:
            logger.warning(
                "OpsAgentBrainAdapter: 超时 session_id=%s error=%s", session_id, exc
            )
            raise BrainUnavailableError(brain_name="ops-agent", reason=f"请求超时: {exc}") from exc
        except httpx.ConnectError as exc:
            logger.warning(
                "OpsAgentBrainAdapter: 连接失败 session_id=%s error=%s", session_id, exc
            )
            raise BrainUnavailableError(
                brain_name="ops-agent", reason=f"无法连接: {exc}"
            ) from exc
        except Exception as exc:
            logger.error(
                "OpsAgentBrainAdapter: 意外错误 session_id=%s type=%s error=%s",
                session_id,
                type(exc).__name__,
                exc,
            )
            raise BrainUnavailableError(
                brain_name="ops-agent", reason=f"{type(exc).__name__}: {exc}"
            ) from exc

    async def submit_acp_response(
        self,
        acp_session_id: str,
        request_id: str,
        outcome: dict[str, Any],
    ) -> bool:
        """提交用户对 _ops/request_input 的响应，唤醒挂起的 Agent。

        由 ConversationService 在收到前端 interactive-response 请求时调用（T-E6）。

        Args:
            acp_session_id: ops-agent ACP session ID（即 htp conversation_id）
            request_id:     ACP request_id（来自 BrainInteractiveRequest.request_id）
            outcome:        用户响应 {"outcome": "selected", "optionId": "1"} 等
        """
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        url = f"{self._base_url}/acp/sessions/{acp_session_id}/responses/{request_id}"
        try:
            resp = await self._client.post(url, json={"result": {"outcome": outcome}}, headers=headers)
            if resp.status_code not in (200, 201):
                logger.error(
                    "OpsAgentBrainAdapter: submit_response 失败 session_id=%s request_id=%s status=%d",
                    acp_session_id,
                    request_id,
                    resp.status_code,
                )
                raise BrainUnavailableError(
                    brain_name="ops-agent",
                    reason=f"submit_response HTTP {resp.status_code}",
                )
        except BrainUnavailableError:
            raise
        except Exception as exc:
            raise BrainUnavailableError(
                brain_name="ops-agent", reason=f"submit_response 失败: {exc}"
            ) from exc
        return True

    async def resume_event_stream(
        self,
        session_id: str,
    ) -> AsyncGenerator[BrainEvent, None]:
        """消费 ops-agent outbox 事件流，不提交新 prompt。

        适用场景：页面刷新后用户提交了 interactive response，
        ops-agent 正在（或已经）处理续写，但前端 SSE 连接已断开。
        通过本方法重新连接 outbox，把续写内容传回前端。

        若 session 不存在，或 session 状态显示 activePrompt=False，
        立即返回（不挂起）。
        """
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        # 检查 session 是否存在且有活跃 prompt，避免无意义的长连接
        try:
            state_resp = await self._client.get(
                f"{self._base_url}/acp/sessions/{session_id}/state",
                headers=headers,
                timeout=5.0,
            )
            if state_resp.status_code == 404:
                logger.info(
                    "OpsAgentBrainAdapter.resume_event_stream: session 不存在，跳过 session_id=%s",
                    session_id,
                )
                return
            if state_resp.status_code == 200 and not state_resp.json().get("activePrompt"):
                logger.info(
                    "OpsAgentBrainAdapter.resume_event_stream: active_prompt=False，跳过 session_id=%s",
                    session_id,
                )
                return
        except Exception as exc:
            logger.warning(
                "OpsAgentBrainAdapter.resume_event_stream: 检查状态失败，跳过 session_id=%s error=%s",
                session_id, exc,
            )
            return

        logger.info(
            "OpsAgentBrainAdapter.resume_event_stream: 开始消费事件 session_id=%s",
            session_id,
        )
        async for event in self._consume_events(session_id, headers):
            yield event

    # ── 私有方法 ──────────────────────────────────────────────────────────────

    async def _ensure_acp_session(self, session_id: str, headers: dict) -> None:
        """幂等创建 ACP 会话（ops-agent 已存在则直接返回）。"""
        url = f"{self._base_url}/acp/sessions"
        payload = {"session_id": session_id}
        resp = await self._client.post(url, json=payload, headers=headers)
        if resp.status_code not in (200, 201):
            raise BrainUnavailableError(
                brain_name="ops-agent",
                reason=f"session_new HTTP {resp.status_code}: {resp.text[:200]}",
            )
        logger.debug(
            "OpsAgentBrainAdapter: ACP 会话就绪 session_id=%s status=%d",
            session_id,
            resp.status_code,
        )

    async def _terminate_acp_prompt(self, session_id: str, headers: dict) -> None:
        """终止 ops-agent session 当前运行的 prompt，并清空 outbox。

        页面刷新恢复场景：session 可能仍有旧 prompt 在等待 _ops/request_input 响应。
        调用此方法取消旧 prompt（向 agent 返回 cancelled），并清空 outbox，
        防止旧 session/done 事件污染随后新 prompt 的 SSE 流。

        失败时仅记录警告，不抛出异常——后续 _submit_prompt 会再次尝试，
        若真的无法恢复再触发 BrainUnavailableError。
        """
        url = f"{self._base_url}/acp/sessions/{session_id}/prompt"
        try:
            resp = await self._client.delete(
                url,
                json={"reason": "客户端恢复对话请求，取消挂起的 prompt", "wait_timeout": 5.0},
                headers=headers,
            )
            if resp.status_code == 404:
                # session 不存在，可能已自动清理，无需处理
                logger.debug(
                    "OpsAgentBrainAdapter: terminate_prompt session 不存在（404），继续重试 session_id=%s",
                    session_id,
                )
                return
            if resp.status_code == 200:
                data = resp.json()
                logger.info(
                    "OpsAgentBrainAdapter: terminate_prompt 成功 session_id=%s "
                    "activePrompt=%s pendingCancelled=%d drained=%d",
                    session_id,
                    data.get("activePrompt"),
                    data.get("pendingRequestsCancelled", 0),
                    data.get("drainedEvents", 0),
                )
            else:
                logger.warning(
                    "OpsAgentBrainAdapter: terminate_prompt 非预期状态 session_id=%s status=%d",
                    session_id,
                    resp.status_code,
                )
        except Exception as exc:
            logger.warning(
                "OpsAgentBrainAdapter: terminate_prompt 调用失败 session_id=%s error=%s（继续重试）",
                session_id,
                exc,
            )

    async def _submit_prompt(
        self, session_id: str, prompt: list[dict], headers: dict
    ) -> None:
        """向 ACP 会话提交 prompt（立即返回 202，Agent 后台执行）。

        若收到 409（session 有旧 prompt 在运行），自动执行 terminate+retry：
        1. 调用 DELETE /acp/sessions/{id}/prompt 终止旧 prompt 并清空 outbox
        2. 重新提交新 prompt
        此机制覆盖"页面刷新后 ops-agent session 仍等待 _ops/request_input"的恢复场景，
        避免直接降级到备用助手。
        """
        url = f"{self._base_url}/acp/sessions/{session_id}/prompt"
        resp = await self._client.post(url, json={"prompt": prompt}, headers=headers)
        if resp.status_code == 409:
            # 会话已有 prompt 在运行（页面刷新恢复场景：agent 正在等待交互响应）
            # 先终止旧 prompt（清空 outbox），再重试新 prompt
            logger.info(
                "OpsAgentBrainAdapter: session 有活跃 prompt（409），执行 terminate+retry session_id=%s",
                session_id,
            )
            await self._terminate_acp_prompt(session_id, headers)
            resp = await self._client.post(url, json={"prompt": prompt}, headers=headers)
            if resp.status_code not in (200, 202):
                raise BrainUnavailableError(
                    brain_name="ops-agent",
                    reason=f"submit_prompt 重试后仍失败 HTTP {resp.status_code}: {resp.text[:200]}",
                )
            return
        if resp.status_code not in (200, 202):
            raise BrainUnavailableError(
                brain_name="ops-agent",
                reason=f"submit_prompt HTTP {resp.status_code}: {resp.text[:200]}",
            )

    async def _consume_events(
        self, session_id: str, headers: dict
    ) -> AsyncGenerator[BrainEvent, None]:
        """消费 GET /acp/sessions/{id}/events SSE 流，翻译为 BrainEvent。

        若 session/done 到达时未产出任何文本内容，抛出 BrainUnavailableError 以触发 HTP fallback，
        避免因 ops-agent 内部静默失败（exception 被 execute_task 吞掉，stop_reason="end_turn"）
        导致前端显示空白气泡。
        """
        url = f"{self._base_url}/acp/sessions/{session_id}/events"
        text_emitted = False  # 追踪是否产出过有效文本
        async with self._client.stream("GET", url, headers=headers) as resp:
            if resp.status_code != 200:
                body = await resp.aread()
                raise BrainUnavailableError(
                    brain_name="ops-agent",
                    reason=f"events SSE HTTP {resp.status_code}: {body.decode()[:200]}",
                )

            async for line in resp.aiter_lines():
                if not line or not line.startswith("data: "):
                    continue  # 跳过心跳注释行（": heartbeat"）和空行

                data_str = line[6:]
                try:
                    data = json.loads(data_str)
                except json.JSONDecodeError:
                    continue

                method = data.get("method", "")

                if method == "session/done":
                    stop_reason = data.get("params", {}).get("stopReason", "end_turn")
                    logger.info(
                        "OpsAgentBrainAdapter: session/done session_id=%s stopReason=%s text_emitted=%s",
                        session_id,
                        stop_reason,
                        text_emitted,
                    )
                    if not text_emitted:
                        # ops-agent 运行结束但没有产出任何文本：
                        # 可能是 execute_task() 内部异常被吞掉、task_done 未被调用、
                        # 或 task_done.summary 为空。抛出 BrainUnavailableError 触发 HTP fallback。
                        raise BrainUnavailableError(
                            brain_name="ops-agent",
                            reason=f"session/done without content (stopReason={stop_reason})，降级到备用助手",
                        )
                    return

                if method == "session/update":
                    update = data.get("params", {}).get("update", {})
                    update_type = update.get("sessionUpdate", "")

                    if update_type == "agent_message_chunk":
                        # Agent 输出文本片段
                        content = update.get("content", {})
                        text = content.get("text", "") if isinstance(content, dict) else str(content)
                        if text:
                            text_emitted = True
                            yield BrainTextChunk(content=text)

                    elif update_type == "session_info_update":
                        # 会话标题更新（前端可选显示进度）
                        title = update.get("title") or ""
                        if title:
                            yield BrainStageUpdate(stage=title)

                elif method == "_ops/request_input":
                    # Agent 请求用户交互（SOP操作卡 / 用户提问）
                    req_id = data.get("id", "")
                    params = data.get("params", {})
                    request = params.get("request", {})
                    yield BrainInteractiveRequest(
                        request_id=req_id,
                        acp_session_id=session_id,
                        kind=request.get("kind", "info_request"),
                        title=request.get("title", ""),
                        prompt=request.get("prompt", ""),
                        options=request.get("options", []),
                        custom_input=request.get("customInput", True),
                        metadata=request.get("_meta", {}),
                    )

    @staticmethod
    def _extract_last_user_message(messages: list[dict[str, Any]]) -> str:
        """提取消息列表中最后一条 user 消息的文本内容。

        ACP session 在 ops-agent 侧保持历史上下文，因此只需传送当前轮次的消息。
        """
        for msg in reversed(messages):
            if msg.get("role") == "user":
                content = msg.get("content", "")
                if isinstance(content, str):
                    return content
                if isinstance(content, list):
                    # OpenAI 多模态格式：[{"type": "text", "text": "..."}]
                    parts = [
                        item.get("text", "")
                        for item in content
                        if isinstance(item, dict) and item.get("type") == "text"
                    ]
                    return "\n".join(p for p in parts if p)
        return ""

    @staticmethod
    def _inject_env_context(user_text: str, env_context: dict[str, Any]) -> str:
        """将 HCI 实时环境上下文注入到用户消息中。"""
        context_lines: list[str] = ["[HCI 环境上下文]"]
        active_alerts = env_context.get("active_alerts", [])
        if active_alerts:
            context_lines.append(f"活跃告警（{len(active_alerts)} 条）：")
            for alert in active_alerts[:5]:  # 最多 5 条，避免 token 爆炸
                context_lines.append(f"  - {alert.get('name', '')}:{alert.get('labels', {})}")
        failed_tasks = env_context.get("failed_tasks", [])
        if failed_tasks:
            context_lines.append(f"失败任务（{len(failed_tasks)} 条）：{failed_tasks[:3]}")
        if len(context_lines) > 1:
            return "\n".join(context_lines) + "\n\n" + user_text
        return user_text

    async def check_health(self) -> bool:
        """检查 ops-agent 服务健康状态。"""
        try:
            resp = await self._client.get(
                f"{self._base_url}/health",
                timeout=httpx.Timeout(connect=3.0, read=5.0, write=3.0, pool=3.0),
            )
            return resp.status_code == 200
        except Exception:
            return False

    async def close(self) -> None:
        """释放 HTTP 客户端资源。"""
        await self._client.aclose()
