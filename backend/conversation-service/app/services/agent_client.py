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
    {"type": "server_restart", "reason": "..."}

``server_restart`` 不由 agent-service 产生，而是本客户端在「上游连接被异常中断」时补发的：
agent-service 滚动发布时 uvicorn 会等在手请求跑完（grace 300s），正常不会断流；一旦断流，
说明对端进程消失（SIGKILL/OOM/网络），推理上下文已无法转移。此时把它报成普通
``error`` 会让用户看到「推理异常」这种错误归因，改为 ``server_restart`` 让调用方
按「换副本重连」处理，日志里保留真实异常类型。
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

    def __init__(self, base_url: str, internal_token: str | None = None) -> None:
        self._base_url = base_url.rstrip("/")
        self._internal_token = internal_token

    def _get_headers(self, extra_headers: dict[str, str] | None = None) -> dict[str, str]:
        """构建包含鉴权和基础元数据的请求头"""
        headers = {"Content-Type": "application/json"}
        if self._internal_token:
            headers["Authorization"] = f"Bearer {self._internal_token}"
        if extra_headers:
            headers.update(extra_headers)
        return headers

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
        headers = self._get_headers({"traceparent": f"00-{active_trace_id}-{span_id}-01"})

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
            # 连不上说明对端副本不存在（发布窗口/实例全退）。按「换副本重连」处理，
            # 由调用方决定是否重试；连续失败则由调用方收敛成错误提示。
            logger.warning(
                event="agent_client_connect_interrupted",
                message=f"无法连接 agent-service，按重启处理: {exc}",
                base_url=self._base_url,
                error_type=type(exc).__name__,
            )
            yield {"type": "server_restart", "reason": type(exc).__name__}
        except (httpx.RemoteProtocolError, httpx.ReadError) as exc:
            # 流式响应中途被对端掐断（进程消失/连接重置）：未收到 done，
            # 已产出的片段仍由调用方落库，前端重连后继续收剩余事件。
            logger.warning(
                event="agent_client_stream_interrupted",
                message="agent-service 流失常中断，按重启处理",
                error_type=type(exc).__name__,
                base_url=self._base_url,
            )
            yield {"type": "server_restart", "reason": type(exc).__name__}
        except Exception as exc:
            # 其余异常是真实错误（鉴权、解析、下游报错），不能伪装成重启。
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
                    headers=self._get_headers(),
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
            async with httpx.AsyncClient(timeout=None) as client, client.stream("GET", url, headers=self._get_headers()) as resp:
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
        except (httpx.ConnectError, httpx.RemoteProtocolError, httpx.ReadError) as exc:
            # 续写流本是长挂起连接（active_prompt=True），Pod 被换时必然中断；
            # 报成 error 会让前端把还在跑的工具调用标成失败，改为让前端退避重连。
            logger.warning(
                event="agent_client_resume_interrupted",
                message="resume_stream 连接中断，按重启处理",
                error_type=type(exc).__name__,
                session_id=session_id,
            )
            yield {"type": "server_restart", "reason": type(exc).__name__}
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
                    headers=self._get_headers(),
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
