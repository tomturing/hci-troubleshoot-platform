---
status: active
category: task
audience: developer
last_updated: 2026-03-28
owner: team
related: 09
---

# Task 09：GLMClient——LLM 专用适配器（P1）

```
你是一名负责 hci-troubleshoot-platform conversation-service LLM 接入的 agent。

【仓库】
git clone https://github.com/tomturing/hci-troubleshoot-platform.git
cd hci-troubleshoot-platform

【背景】
当前系统通过 OpenAI SDK 调用 GLM（通过 OpenClaw 代理），但存在以下问题：
  1. GLM 偶尔返回非标准 JSON 格式的 tool_calls 参数（缺少引号/尾随逗号）
  2. GLM 的 function_call 格式与 OpenAI 规范有细微差异
  3. 流式输出的 tool_calls 合并逻辑与标准 OpenAI 不完全相同
  4. 没有错误码重试逻辑（429 限流/502 网关超时）
  5. 没有 usage 统计（无法追踪 token 消耗）

需要封装一个专用的 GLMClient 处理以上差异，并作为 ReactExecutor 的唯一 LLM 入口。

LLM 配置（从环境变量读取）：
  OPENCLAW_BASE_URL  → GLM 服务地址（OpenAI 兼容格式）
  OPENCLAW_API_KEY   → API Key
  GLM_MODEL          → 模型名称（如 glm-4-flash）

【任务目标】
1. 实现 backend/conversation-service/app/core/glm_client.py
2. 处理 GLM 特有的 JSON 修复（工具调用参数）
3. 实现流式 + 非流式两种调用模式
4. 实现指数退避重试（429/502 错误）
5. 每次调用记录 token usage 到 trace 日志

【涉及服务 / 文件范围】
允许新建/修改：
  - backend/conversation-service/app/core/glm_client.py（新建）
  - backend/conversation-service/app/core/__init__.py
只读参考：
  - docs/architecture/各层最优设计.md § Layer 2（GLMClient 代码示例）
  - .env / deploy/env/platform.env（查看现有环境变量名称，不修改）
禁止：
  - 在代码中硬编码任何 API Key 或 Base URL

【详细实现步骤】

Step 1：实现 GLMClient

```python
# backend/conversation-service/app/core/glm_client.py
"""GLM 专用 LLM 客户端，处理与标准 OpenAI 的格式差异"""
import asyncio
import json
import logging
import re
from typing import AsyncGenerator
from pydantic import BaseModel
from openai import AsyncOpenAI, RateLimitError, APIConnectionError

logger = logging.getLogger(__name__)

class ToolCall(BaseModel):
    """工具调用结构"""
    id: str
    name: str
    args: dict

class LLMResponse(BaseModel):
    """GLM 响应标准化结构"""
    content: str | None
    finish_reason: str       # stop | tool_calls | length
    tool_calls: list[ToolCall]
    usage: dict

class GLMClient:
    """GLM 专用客户端"""

    MAX_RETRIES = 3
    RETRY_DELAY_BASE = 1.0   # 指数退避基数（秒）

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
        stream: bool = False,
    ) -> LLMResponse:
        """同步调用（适用于 ReAct 推理步骤）"""
        params = self._build_params(messages, tools, stream=False)

        for attempt in range(self.MAX_RETRIES):
            try:
                resp = await self.client.chat.completions.create(**params)
                result = self._parse_response(resp)
                # 记录 usage
                logger.info(
                    "GLM 调用完成",
                    extra={
                        "model": self.model,
                        "usage": result.usage,
                        "has_tool_calls": bool(result.tool_calls),
                        "finish_reason": result.finish_reason,
                    }
                )
                return result
            except RateLimitError:
                if attempt < self.MAX_RETRIES - 1:
                    wait = self.RETRY_DELAY_BASE * (2 ** attempt)
                    logger.warning(f"GLM 限流，{wait}s 后重试（第 {attempt+1} 次）")
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
        async with await self.client.chat.completions.create(**params) as stream:
            async for chunk in stream:
                delta = chunk.choices[0].delta if chunk.choices else None
                if delta and delta.content:
                    yield delta.content

    def _build_params(self, messages, tools, stream) -> dict:
        params = {
            "model": self.model,
            "messages": messages,
            "stream": stream,
            "temperature": 0.1,       # 排障场景需要确定性输出
            "max_tokens": 4096,
        }
        if tools:
            params["tools"] = tools
            params["tool_choice"] = "auto"
        return params

    def _parse_response(self, resp) -> LLMResponse:
        choice = resp.choices[0]
        tool_calls = []

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
        """安全解析 JSON，处理 GLM 偶尔的非标准格式"""
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass

        # 修复1：移除尾随逗号
        fixed = re.sub(r',\s*([}\]])', r'\1', raw)
        # 修复2：补全未闭合的引号（简单处理）
        try:
            return json.loads(fixed)
        except json.JSONDecodeError:
            logger.warning(f"无法解析 tool_call JSON [id={call_id}]: {raw[:100]}")
            return {"_raw": raw}   # 降级：保留原始字符串

    @classmethod
    def from_env(cls) -> "GLMClient":
        """从环境变量创建实例"""
        import os
        return cls(
            base_url=os.environ["OPENCLAW_BASE_URL"],
            api_key=os.environ["OPENCLAW_API_KEY"],
            model=os.environ.get("GLM_MODEL", "glm-4-flash"),
        )
```

Step 2：单元测试

tests/unit/test_glm_client.py：
  - 测试 _safe_parse_json 对尾随逗号/缺少引号的处理
  - 使用 AsyncMock mock OpenAI client 测试 chat() 方法
  - 测试 429 重试逻辑（mock RateLimitError，验证 sleep 调用次数）
  - 测试 tool_calls 列表的正确解析

Step 3：集成到 conversation_service.py

替换现有的 OpenAI client 使用，统一通过 GLMClient 调用：
```python
# 在 conversation_service.py 的依赖注入或初始化中
self.glm_client = GLMClient.from_env()
# 确保 OPENCLAW_BASE_URL、OPENCLAW_API_KEY 在 platform.env 中已有定义（不新增）
```

【约束】
- API Key 和 Base URL 只从环境变量读取，不硬编码
- _safe_parse_json 不能抛异常（降级为保留 _raw 字段）
- 重试间隔不超过 8 秒（RETRY_DELAY_BASE × 2^2）

【验收标准】
- [ ] uv run pytest tests/unit/test_glm_client.py -v 全通过，含 JSON 修复用例
- [ ] 重试逻辑：429 时最多重试 3 次，每次延迟翻倍
- [ ] 连接 OpenClaw 后发送真实请求，token usage 出现在日志中
- [ ] make lint 无新增错误
```

---