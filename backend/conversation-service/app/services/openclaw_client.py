"""
OpenClaw Client - OpenAI-compatible API Client
"""

import httpx
from typing import AsyncGenerator, Dict, Any, List, Optional
import json
import asyncio

from shared.utils.logger import get_logger

logger = get_logger("openclaw-client")

class OpenClawClient:
    """
    OpenClaw API客户端 (OpenAI兼容)
    使用 /v1/chat/completions 端点
    """
    
    def __init__(self, base_url: str, api_key: Optional[str] = None):
        self.base_url = base_url.rstrip('/')
        self.api_key = api_key
        self.client = httpx.AsyncClient(timeout=60.0)
    
    async def chat_completion_stream(
        self,
        messages: List[Dict[str, str]],
        user_id: str,
        model: str = "openclaw",
        trace_id: Optional[str] = None
    ) -> AsyncGenerator[str, None]:
        """
        调用OpenClaw Chat Completions API (流式)
        
        Args:
            messages: 消息列表 [{"role": "user", "content": "..."}]
            user_id: 用户ID (映射为OpenClaw Session Key)
            model: 模型名称 (默认为 "openclaw" 或 "agent:<agentId>")
            trace_id: 全链路追踪ID
            
        Yields:
            str: AI响应内容片段
        """
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}" if self.api_key else ""
        }
        
        payload = {
            "model": model,
            "messages": messages,
            "stream": True,
            "user": user_id  # 映射为Session Key
        }
        
        url = f"{self.base_url}/v1/chat/completions"
        
        logger.info(
            event="openclaw_request",
            message="Sending request to OpenClaw",
            url=url,
            user_id=user_id,
            trace_id=trace_id
        )
        
        try:
            async with self.client.stream(
                "POST",
                url,
                json=payload,
                headers=headers
            ) as response:
                if response.status_code != 200:
                    error_body = await response.aread()
                    logger.error(
                        event="openclaw_error",
                        message=f"OpenClaw returned status {response.status_code}",
                        status=response.status_code,
                        body=error_body.decode('utf-8', errors='ignore'),
                        trace_id=trace_id
                    )
                    response.raise_for_status()
                
                async for line in response.aiter_lines():
                    if not line or not line.startswith("data: "):
                        continue
                        
                    data_str = line[6:]  # Skip "data: "
                    if data_str.strip() == "[DONE]":
                        break
                        
                    try:
                        data = json.loads(data_str)
                        delta = data.get("choices", [{}])[0].get("delta", {})
                        content = delta.get("content", "")
                        if content:
                            yield content
                    except json.JSONDecodeError:
                        continue
                        
        except Exception as e:
            logger.error(
                event="openclaw_exception",
                message="Error calling OpenClaw API",
                error=str(e),
                trace_id=trace_id
            )
            raise

    async def check_health(self) -> bool:
        """检查OpenClaw服务健康状态。

        说明：真实 OpenClaw Gateway 并不一定提供 GET /v1/models 或 /health。
        为了在不依赖上游模型 Key 的情况下验证“网关可达 + 鉴权可用”，这里对
        /v1/chat/completions 发送一个必定不合法的 payload，预期返回 400/422。
        """

        url = f"{self.base_url}/v1/chat/completions"
        headers = {
            "Content-Type": "application/json",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        try:
            response = await self.client.post(url, json={}, headers=headers)
            if response.status_code in (200, 400, 422):
                return True
            if response.status_code in (401, 403, 404, 405):
                return False
            # 其他状态码（如 500）表示服务可达但内部异常，按不健康处理
            return False
        except Exception:
            return False

    async def close(self):
        """关闭客户端"""
        await self.client.aclose()
