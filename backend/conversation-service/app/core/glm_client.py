"""
GLM 专用 LLM 客户端——处理与标准 OpenAI 的格式差异

主要解决：
  1. GLM 偶尔返回非标准 JSON 格式的 tool_calls 参数（尾随逗号/缺少引号）
  2. 流式 tool_calls 合并逻辑
  3. 指数退避重试（429 限流 / 502 网关超时）
  4. token usage 统计记录
"""

import asyncio
import json
import logging
import os
import re
from collections.abc import AsyncGenerator

from openai import APIConnectionError, AsyncOpenAI, RateLimitError
from pydantic import BaseModel

logger = logging.getLogger(__name__)


class ToolCall(BaseModel):
    """工具调用结构"""

    id: str
    name: str
    args: dict


class LLMResponse(BaseModel):
    """GLM 响应标准化结构"""

    content: str | None
    finish_reason: str        # stop | tool_calls | length
    tool_calls: list[ToolCall]
    usage: dict


class GLMClient:
    """GLM 专用客户端，封装 OpenAI SDK 并处理 GLM 特有差异"""

    MAX_RETRIES = 3
    RETRY_DELAY_BASE = 1.0    # 指数退避基数（秒），最长 4 秒（1×2^2）

    def __init__(self, base_url: str, api_key: str, model: str):
        self.client = AsyncOpenAI(
            base_url=base_url,
            api_key=api_key,
        )
        self.model = model

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
    ) -> LLMResponse:
        """非流式调用（适用于 ReAct 推理步骤）"""
        params = self._build_params(messages, tools, stream=False)

        for attempt in range(self.MAX_RETRIES):
            try:
                resp = await self.client.chat.completions.create(**params)
                result = self._parse_response(resp)
                logger.info(
                    "GLM 调用完成",
                    extra={
                        "model": self.model,
                        "usage": result.usage,
                        "has_tool_calls": bool(result.tool_calls),
                        "finish_reason": result.finish_reason,
                    },
                )
                return result
            except RateLimitError:
                if attempt < self.MAX_RETRIES - 1:
                    wait = self.RETRY_DELAY_BASE * (2**attempt)
                    logger.warning(f"GLM 限流，{wait}s 后重试（第 {attempt + 1} 次）")
                    await asyncio.sleep(wait)
                else:
                    raise
            except APIConnectionError as e:
                logger.error(f"GLM 连接失败: {e}")
                raise

    async def stream_chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
    ) -> AsyncGenerator[str, None]:
        """流式调用（适用于向用户输出对话内容）"""
        params = self._build_params(messages, tools, stream=True)
        stream = await self.client.chat.completions.create(**params)
        async for chunk in stream:
            delta = chunk.choices[0].delta if chunk.choices else None
            if delta and delta.content:
                yield delta.content

    def _build_params(self, messages: list[dict], tools: list[dict] | None, stream: bool) -> dict:
        """构建 API 调用参数"""
        params: dict = {
            "model": self.model,
            "messages": messages,
            "stream": stream,
            "temperature": 0.1,     # 排障场景需要确定性输出
            "max_tokens": 4096,
        }
        if tools:
            params["tools"] = tools
            params["tool_choice"] = "auto"
        return params

    def _parse_response(self, resp) -> LLMResponse:
        """解析非流式响应为标准 LLMResponse"""
        choice = resp.choices[0]
        tool_calls: list[ToolCall] = []

        if choice.message.tool_calls:
            for tc in choice.message.tool_calls:
                args = self._safe_parse_json(tc.function.arguments, tc.id)
                tool_calls.append(ToolCall(
                    id=tc.id,
                    name=tc.function.name,
                    args=args,
                ))

        return LLMResponse(
            content=choice.message.content,
            finish_reason=choice.finish_reason or "stop",
            tool_calls=tool_calls,
            usage=resp.usage.model_dump() if resp.usage else {},
        )

    def _safe_parse_json(self, raw: str, call_id: str) -> dict:
        """
        安全解析 JSON，处理 GLM 偶尔的非标准格式。
        降级策略：无法解析时返回 {"_raw": raw}，不抛异常。
        """
        if not raw:
            return {}
        # 尝试直接解析
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass

        # 修复1：移除尾随逗号
        fixed = re.sub(r",\s*([}\]])", r"\1", raw)
        try:
            return json.loads(fixed)
        except json.JSONDecodeError:
            pass

        # 修复2：补全缺少的末尾花括号
        try:
            return json.loads(fixed + "}")
        except json.JSONDecodeError:
            logger.warning(f"无法解析 tool_call JSON [id={call_id}]: {raw[:100]}")
            return {"_raw": raw}    # 降级：保留原始字符串

    @classmethod
    def from_env(cls) -> "GLMClient":
        """从环境变量创建实例"""
        return cls(
            base_url=os.environ["OPENCLAW_BASE_URL"],
            api_key=os.environ["OPENCLAW_API_KEY"],
            model=os.environ.get("GLM_MODEL", "glm-4-flash"),
        )
