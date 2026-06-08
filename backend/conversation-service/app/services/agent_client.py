"""
AgentClient - HTTP SSE 客户端

conversation-service 通过此客户端将推理请求委托给 agent-service，
接收 SSE 事件流并转发给前端。

事件格式（与 agent-service routes/agent.py 保持严格一致）：
    {"type": "text_chunk", "content": "..."}
    {"type": "stage_update", "stage": "S2", "metadata": {}}
    {"type": "interactive_request", "request_id": "...", ...}
    {"type": "escalation", "reason": "...", "context": {}}
    {"type": "done"}
    {"type": "error", "message": "..."}
"""

from __future__ import annotations

import json
import secrets
from collections.abc import AsyncGenerator
from typing import Any

import httpx
from shared.observability.logger import get_logger
from shared.observability.otel import get_current_trace_id

logger = get_logger("agent_client")


class AgentClient:
    """
    agent-service 的 HTTP SSE 客户端。

    与 conversation-service 原有 AgentRouter 接口对齐，
    使调用方切换成本最小化。
    """

    def __init__(self, base_url: str) -> None:
        self._base_url = base_url.rstrip("/")

    async def stream(
        self,
        assistant_type: str,
        session_id: str,
        case_id: str,
        user_id: str,
        messages: list[dict[str, Any]],
        env_context: dict[str, Any] | None = None,
        stream: bool = True,
        diagnostic_stage: str = "S0",  # 诊断阶段
        category_id: str | None = None,  # S0 确认的分类
        execution_mode: str = "direct",  # 执行模式
        system_prompt: str | None = None,  # 自定义 system_prompt
        sop_resume_context: dict[str, Any] | None = None,  # T-AGT-23: SOP 执行恢复上下文
    ) -> AsyncGenerator[dict[str, Any], None]:
        """
        调用 agent-service POST /v1/agent/stream，以异步生成器方式 yield 事件 dict。

        Yields:
            各类事件 dict，字段含 ``type`` 键，如：
            ``{"type": "text_chunk", "content": "..."}``
        """
        payload = {
            "assistant_type": assistant_type,
            "session_id": session_id,
            "case_id": case_id,
            "user_id": user_id,
            "messages": messages,
            "env_context": env_context,
            "stream": stream,
            "diagnostic_stage": diagnostic_stage,
            "category_id": category_id,
            "execution_mode": execution_mode,
            "system_prompt": system_prompt,
            "sop_resume_context": sop_resume_context,  # T-AGT-23
        }

        url = f"{self._base_url}/v1/agent/stream"
        logger.debug(
            event="agent_client_request",
            message="发送推理请求",
            url=url,
            assistant_type=assistant_type,
            session_id=session_id,
        )

        # 强行注入 traceparent 头以避免 Starlette streaming 异步迭代器中 ContextVars 丢失导致 Trace 分叉
        active_trace_id = get_current_trace_id() or secrets.token_hex(16)
        span_id = secrets.token_hex(8)
        headers = {"traceparent": f"00-{active_trace_id}-{span_id}-01", "Content-Type": "application/json"}

        try:
            async with (
                httpx.AsyncClient(timeout=None) as client,
                client.stream("POST", url, json=payload, headers=headers) as resp,
            ):
                if resp.status_code != 200:
                    body = await resp.aread()
                    logger.error(
                        event="agent_client_http_error",
                        message=f"agent-service 返回 {resp.status_code}",
                        status_code=resp.status_code,
                        body=body.decode(errors="replace")[:500],
                    )
                    yield {
                        "type": "error",
                        "message": f"agent-service HTTP {resp.status_code}",
                    }
                    return

                async for line in resp.aiter_lines():
                    line = line.strip()
                    if not line or not line.startswith("data: "):
                        continue
                    raw = line[6:]  # 去掉 "data: " 前缀
                    try:
                        event = json.loads(raw)
                    except json.JSONDecodeError:
                        logger.warning(
                            event="agent_client_parse_error",
                            message=f"无法解析 SSE 行: {raw[:200]}",
                        )
                        continue

                    yield event

                    # "done" 事件后退出迭代
                    if event.get("type") == "done":
                        break

        except httpx.ConnectError as exc:
            logger.error(
                event="agent_client_connect_error",
                message=f"无法连接 agent-service: {exc}",
                base_url=self._base_url,
            )
            yield {"type": "error", "message": f"无法连接 agent-service: {exc}"}
        except Exception as exc:
            logger.error(
                event="agent_client_error",
                message=f"AgentClient 异常: {exc}",
            )
            yield {"type": "error", "message": f"推理请求异常: {exc}"}

    async def submit_interactive_response(
        self,
        acp_session_id: str,
        request_id: str,
        outcome: dict,
    ) -> bool:
        """
        转发 ACP 交互响应到 agent-service。

        agent-service 再将其转发给 ops-agent。
        """
        url = f"{self._base_url}/v1/agent/interactive-response"
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    url,
                    json={
                        "acp_session_id": acp_session_id,
                        "request_id": request_id,
                        "outcome": outcome,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                return bool(data.get("success", False))
        except Exception as exc:
            logger.error(
                event="agent_client_interactive_error",
                message=f"submit_interactive_response 失败: {exc}",
                acp_session_id=acp_session_id,
            )
            return False

    async def resume_stream(
        self,
        session_id: str,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """
        调用 agent-service GET /v1/agent/resume-stream/{session_id}，
        获取 ops-agent 待处理事件流。
        """
        url = f"{self._base_url}/v1/agent/resume-stream/{session_id}"
        try:
            async with httpx.AsyncClient(timeout=None) as client, client.stream("GET", url) as resp:
                if resp.status_code != 200:
                    logger.warning(
                        event="agent_client_resume_error",
                        message=f"resume-stream HTTP {resp.status_code}",
                        session_id=session_id,
                    )
                    return
                async for line in resp.aiter_lines():
                    line = line.strip()
                    if not line or not line.startswith("data: "):
                        continue
                    raw = line[6:]
                    try:
                        event = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    yield event
                    if event.get("type") == "done":
                        break
        except Exception as exc:
            logger.error(
                event="agent_client_resume_exception",
                message=f"resume_stream 异常: {exc}",
                session_id=session_id,
            )

    async def react_confirm(
        self,
        session_id: str,
        confirmed: bool,
        authorized_by: str = "user",
        exec_id: str | None = None,
    ) -> bool:
        """
        调用 agent-service POST /v1/agent/react-confirm，
        提交 ReAct 工具确认结果。

        当 ReactEngine 对 risk_level>=2 的工具调用暂停等待用户确认时，
        前端通过 conversation-service 路由到此方法。

        Args:
            session_id: 会话 ID（对应 conversation_id）
            confirmed: True=用户确认执行，False=用户取消
            authorized_by: 操作人标识（默认"user"）
            exec_id: 工具执行记录 ID

        Returns:
            True = 提交成功；False = ConfirmService 未注入或请求失败
        """
        url = f"{self._base_url}/v1/agent/react-confirm"
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    url,
                    json={
                        "session_id": session_id,
                        "confirmed": confirmed,
                        "authorized_by": authorized_by,
                        "exec_id": exec_id,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                return bool(data.get("ok", False))
        except Exception as exc:
            logger.error(
                event="agent_client_react_confirm_error",
                message=f"react_confirm 失败: {exc}",
                session_id=session_id,
                exec_id=exec_id,
            )
            return False
