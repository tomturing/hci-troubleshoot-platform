"""
OpsAgentBrainAdapter：ops-agent 大脑的 BrainPort 实现（T1-5：反腐层）

替代 ai_client.py 中的 OpsAgentAssistant 类。核心改进：
- 传递 session_id（多轮上下文连续性）
- 传递 hci_context（实时告警/失败任务注入）
- ops-agent 不可达时 raise BrainUnavailableError（不透传原始异常）
- 解析 x_stage_update 扩展字段（阶段变化通知）

跨仓库接口依据：docs/contracts/brain-http-api.yaml
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import AsyncGenerator
from typing import Any

import httpx

from app.core.brain_port import BrainEvent, BrainStageUpdate, BrainTextChunk, BrainUnavailableError

logger = logging.getLogger("ops-agent-brain-adapter")


class OpsAgentBrainAdapter:
    """ops-agent 大脑的 BrainPort 适配器（反腐层）。

    通过 HTTP 调用 ops-agent 的 /v1/chat/completions 端点，
    将 ops-agent 的 SSE 流转换为 BrainEvent 序列。

    接口契约：docs/contracts/brain-http-api.yaml
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
        _read_timeout = float(os.environ.get("AI_CLIENT_READ_TIMEOUT_SEC", "180.0"))
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
        """调用 ops-agent HTTP API，以流式 BrainEvent 产出响应。

        Args:
            session_id: 对话 session ID，传给 ops-agent 实现多轮上下文恢复。
            messages: OpenAI 格式消息列表。
            env_context: HCI 实时环境上下文（active_alerts / failed_tasks / env_info）。
            stream: 是否流式输出（始终 True，ops-agent 只支持流式）。
            user_id: 用户 ID，用于日志关联。

        Yields:
            BrainTextChunk: LLM 输出的文本片段
            BrainStageUpdate: ops-agent 阶段变化（x_stage_update 扩展字段）

        Raises:
            BrainUnavailableError: ops-agent 服务不可达或返回非 2xx 状态码。
        """
        url = f"{self._base_url}/v1/chat/completions"
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        # 构造请求体（包含 session_id 和 hci_context 扩展字段）
        payload: dict[str, Any] = {
            "model": self._default_model,
            "messages": messages,
            "stream": True,
            "user": user_id or session_id,
            "session_id": session_id,
        }
        if env_context:
            payload["hci_context"] = env_context

        logger.info(
            "OpsAgentBrainAdapter: sending request url=%s session_id=%s has_env=%s",
            url,
            session_id,
            env_context is not None,
        )

        try:
            async with self._client.stream("POST", url, json=payload, headers=headers) as resp:
                if resp.status_code != 200:
                    body = await resp.aread()
                    error_text = body.decode("utf-8", errors="replace")
                    logger.error(
                        "OpsAgentBrainAdapter: HTTP %d session_id=%s body=%s",
                        resp.status_code,
                        session_id,
                        error_text[:200],
                    )
                    raise BrainUnavailableError(
                        brain_name="ops-agent",
                        reason=f"HTTP {resp.status_code}: {error_text[:200]}",
                    )

                stream_start = time.monotonic()
                ttft_logged = False

                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data: "):
                        continue
                    data_str = line[6:]
                    if data_str.strip() == "[DONE]":
                        return

                    try:
                        data = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue

                    # 解析 x_stage_update 扩展字段（阶段变化通知）
                    stage_update = data.get("x_stage_update")
                    if stage_update and isinstance(stage_update, dict):
                        stage = stage_update.get("current_stage", "")
                        if stage:
                            yield BrainStageUpdate(
                                stage=stage,
                                metadata={k: v for k, v in stage_update.items() if k != "current_stage"},
                            )

                    # 解析文本内容
                    choices = data.get("choices", [])
                    if not choices:
                        continue
                    delta = choices[0].get("delta", {})
                    content = delta.get("content", "")
                    if content:
                        if not ttft_logged:
                            ttft_ms = int((time.monotonic() - stream_start) * 1000)
                            logger.info(
                                "OpsAgentBrainAdapter TTFT: %dms session_id=%s",
                                ttft_ms,
                                session_id,
                            )
                            ttft_logged = True
                        yield BrainTextChunk(content=content)

        except BrainUnavailableError:
            raise
        except httpx.TimeoutException as exc:
            logger.warning(
                "OpsAgentBrainAdapter: timeout session_id=%s error=%s",
                session_id,
                exc,
            )
            raise BrainUnavailableError(
                brain_name="ops-agent",
                reason=f"请求超时: {exc}",
            ) from exc
        except httpx.ConnectError as exc:
            logger.warning(
                "OpsAgentBrainAdapter: connect error session_id=%s error=%s",
                session_id,
                exc,
            )
            raise BrainUnavailableError(
                brain_name="ops-agent",
                reason=f"无法连接: {exc}",
            ) from exc
        except Exception as exc:
            logger.error(
                "OpsAgentBrainAdapter: unexpected error session_id=%s type=%s error=%s",
                session_id,
                type(exc).__name__,
                exc,
            )
            raise BrainUnavailableError(
                brain_name="ops-agent",
                reason=f"{type(exc).__name__}: {exc}",
            ) from exc

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
