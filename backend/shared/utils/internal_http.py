"""
内部服务 HTTP 客户端基类（G-3）

统一处理服务间调用的认证头注入，避免各 client 重复实现：
  - 自动注入 Bearer token（INTERNAL_API_TOKEN 环境变量）
  - 注入 X-Service-Name 请求头便于链路追踪
  - 统一超时配置入口

用法：
    from shared.utils.internal_http import InternalHTTPClient

    class KBClient(InternalHTTPClient):
        def __init__(self, base_url: str):
            super().__init__(base_url, timeout=10.0)

        async def search(self, query: str) -> list:
            resp = await self.post("/api/kb/search", json={"query": query})
            resp.raise_for_status()
            return resp.json().get("chunks", [])
"""

import os

import httpx

from shared.utils.logger import get_logger

logger = get_logger("internal-http-client")


class InternalHTTPClient:
    """
    服务间内部调用 HTTP 客户端基类。
    自动注入认证 Token 和服务名，避免逐调用点重复实现（G-3）。

    每个实例持有一个长连接 AsyncClient（复用连接池），
    调用方负责在合适时机调用 aclose()，或使用 async with 上下文管理器。
    """

    def __init__(self, base_url: str, timeout: float | None = 30.0):
        token = os.environ.get("INTERNAL_API_TOKEN", "")
        service_name = os.environ.get("SERVICE_NAME", "unknown")
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {token}",
                "X-Service-Name": service_name,
                "Content-Type": "application/json",
            },
        )

    async def get(self, path: str, **kwargs) -> httpx.Response:
        """发送 GET 请求"""
        return await self._client.get(path, **kwargs)

    async def post(self, path: str, **kwargs) -> httpx.Response:
        """发送 POST 请求"""
        return await self._client.post(path, **kwargs)

    async def put(self, path: str, **kwargs) -> httpx.Response:
        """发送 PUT 请求"""
        return await self._client.put(path, **kwargs)

    async def delete(self, path: str, **kwargs) -> httpx.Response:
        """发送 DELETE 请求"""
        return await self._client.delete(path, **kwargs)

    async def aclose(self) -> None:
        """关闭连接池（在服务关闭时调用）"""
        await self._client.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.aclose()
