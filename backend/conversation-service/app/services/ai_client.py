"""
AI Assistant Client - 多类型AI助手客户端抽象层 (v2.0)

支持通过 Protocol + Factory 模式扩展多种AI助手后端。
当前实现: OpenClaw (OpenAI兼容API)
"""

import json
from collections.abc import AsyncGenerator
from typing import Protocol, runtime_checkable

import httpx
from shared.utils.logger import get_logger

logger = get_logger("ai-client")


@runtime_checkable
class AIAssistantClient(Protocol):
    """AI助手客户端协议 — 所有助手后端必须实现此接口"""

    async def chat_completion_stream(
        self, messages: list[dict[str, str]], user_id: str, pod_endpoint: str | None = None, model: str = ""
    ) -> AsyncGenerator[str, None]:
        """流式对话补全"""
        ...

    async def check_health(self) -> bool:
        """健康检查"""
        ...

    async def close(self) -> None:
        """关闭客户端资源"""
        ...


class OpenClawAssistant:
    """
    OpenClaw AI助手客户端 (OpenAI兼容)
    使用 /v1/chat/completions 端点
    """

    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        default_model: str = "openclaw",
        assistant_type: str = "openclaw",
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.default_model = default_model
        self.assistant_type = assistant_type
        self.client = httpx.AsyncClient(timeout=60.0)

    async def chat_completion_stream(
        self, messages: list[dict[str, str]], user_id: str, pod_endpoint: str | None = None, model: str = "openclaw"
    ) -> AsyncGenerator[str, None]:
        """
        调用OpenClaw Chat Completions API (流式)

        Args:
            messages: 消息列表 [{"role": "user", "content": "..."}]
            user_id: 用户ID (映射为OpenClaw Session Key)
            model: 模型名称

        Yields:
            str: AI响应内容片段
        """
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}" if self.api_key else "",
        }

        payload = {"model": model or self.default_model, "messages": messages, "stream": True, "user": user_id}

        target_base_url = (pod_endpoint or self.base_url).rstrip("/")
        url = f"{target_base_url}/v1/chat/completions"

        logger.info(
            event="ai_request",
            message="Sending request to OpenClaw",
            url=url,
            user_id=user_id,
            assistant_type=self.assistant_type,
        )

        try:
            async with self.client.stream("POST", url, json=payload, headers=headers) as response:
                if response.status_code != 200:
                    error_body = await response.aread()
                    logger.error(
                        event="ai_error",
                        message=f"OpenClaw returned status {response.status_code}",
                        status=response.status_code,
                        body=error_body.decode("utf-8", errors="ignore"),
                    )
                    response.raise_for_status()

                async for line in response.aiter_lines():
                    if not line or not line.startswith("data: "):
                        continue

                    data_str = line[6:]  # 跳过 "data: "
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
            logger.error(event="ai_exception", message="Error calling OpenClaw API", error=str(e))
            raise

    async def check_health(self) -> bool:
        """检查OpenClaw服务健康状态"""
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
            return False
        except Exception:
            return False

    async def close(self):
        """关闭客户端"""
        await self.client.aclose()


class AIAssistantRegistry:
    """AI助手客户端注册表 (v2.0)

    管理多种AI助手客户端实例。通过 assistant_type 查找对应的客户端。
    """

    def __init__(self):
        self._clients: dict[str, AIAssistantClient] = {}
        self._default_type: str = "openclaw"

    def register(self, assistant_type: str, client: AIAssistantClient) -> None:
        """注册AI助手客户端"""
        self._clients[assistant_type] = client
        logger.info(f"Registered AI assistant client: {assistant_type}")

    def get_client(self, assistant_type: str | None = None) -> AIAssistantClient | None:
        """获取指定类型的AI助手客户端"""
        atype = assistant_type or self._default_type
        client = self._clients.get(atype)
        if not client:
            logger.warning(f"No AI assistant client found for type: {atype}")
        return client

    def list_types(self) -> list[str]:
        """列出所有已注册的助手类型"""
        return list(self._clients.keys())

    async def close_all(self) -> None:
        """关闭所有客户端"""
        for atype, client in self._clients.items():
            try:
                await client.close()
                logger.info(f"Closed AI assistant client: {atype}")
            except Exception as e:
                logger.error(f"Error closing AI assistant client {atype}: {e}")
        self._clients.clear()

    async def health_check_all(self) -> dict[str, bool]:
        """对所有注册客户端执行健康检查"""
        results = {}
        for atype, client in self._clients.items():
            try:
                results[atype] = await client.check_health()
            except Exception:
                results[atype] = False
        return results


def create_openclaw_client(
    base_url: str,
    api_key: str | None = None,
    default_model: str = "openclaw",
    assistant_type: str = "openclaw",
) -> OpenClawAssistant:
    """工厂函数: 创建OpenClaw助手客户端"""
    return OpenClawAssistant(
        base_url=base_url,
        api_key=api_key,
        default_model=default_model,
        assistant_type=assistant_type,
    )
