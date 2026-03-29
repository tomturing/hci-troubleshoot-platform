"""
KB Client - 知识库服务客户端
负责调用 kb-service 进行混合检索和 SOP 匹配
"""

import httpx
from shared.utils.internal_http import InternalHTTPClient
from shared.utils.logger import get_logger

logger = get_logger("conversation-kb-client")

# 超时配置（KB 检索通常需要向量计算，给予充足时间）
_REQUEST_TIMEOUT = 10.0


class KBClient(InternalHTTPClient):
    """
    知识库服务 HTTP 客户端（G-3：继承 InternalHTTPClient，统一认证头管理）

    持有长连接连接池，避免每次请求创建新 AsyncClient。
    调用方应在服务关闭时调用 await kb_client.aclose()。
    """

    def __init__(self, kb_service_url: str, internal_token: str):
        import os
        # 优先使用传入的 internal_token（兼容现有初始化方式），
        # InternalHTTPClient 从 INTERNAL_API_TOKEN 环境变量读取；
        # 若 token 已通过参数传入，暂时注入环境变量供基类读取。
        os.environ.setdefault("INTERNAL_API_TOKEN", internal_token)
        if internal_token:
            os.environ["INTERNAL_API_TOKEN"] = internal_token
        super().__init__(base_url=kb_service_url.rstrip("/"), timeout=_REQUEST_TIMEOUT)
        # 兼容旧代码中直接访问 _base_url 的地方
        self._api_prefix = "/api/kb"
        self._atoms_prefix = "/api/v1/atoms"

    async def search(self, query: str, top_n: int = 5) -> list[dict]:
        """
        混合检索（BM25 + 向量 RRF 融合）

        返回 ChunkResult 列表，每项包含：
          - chunk_id, document_id, content, score, source_title, source_type, page_num
        """
        try:
            resp = await self.post(
                f"{self._api_prefix}/search",
                json={"query": query, "top_n": top_n},
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("chunks", [])
        except httpx.HTTPStatusError as exc:
            logger.warning(
                event="kb_search_http_error",
                message=f"KB search returned HTTP {exc.response.status_code}",
                query=query[:80],
                status_code=exc.response.status_code,
            )
            return []
        except httpx.RequestError as exc:
            logger.warning(
                event="kb_search_unavailable",
                message=f"KB service unreachable: {exc}",
                query=query[:80],
            )
            return []

    async def sop_match(self, query: str) -> dict | None:
        """
        SOP 关键词精确匹配

        返回命中节点的完整内容，未命中返回 None：
          - node_id, title, content, category, keywords
        """
        try:
            resp = await self.post(
                f"{self._api_prefix}/sop/match",
                json={"query": query},
            )
            resp.raise_for_status()
            data = resp.json()
            # 未命中时 matched=false
            if not data.get("matched"):
                return None
            return data
        except httpx.HTTPStatusError as exc:
            logger.warning(
                event="kb_sop_http_error",
                message=f"KB SOP match returned HTTP {exc.response.status_code}",
                query=query[:80],
                status_code=exc.response.status_code,
            )
            return None
        except httpx.RequestError as exc:
            logger.warning(
                event="kb_sop_unavailable",
                message=f"KB service unreachable: {exc}",
                query=query[:80],
            )
            return None

    async def search_atoms(
        self,
        query: str,
        category_id: str | None = None,
        task_error_keywords: list[str] | None = None,
        hci_version: str | None = None,
        top_k: int = 5,
    ) -> list[dict]:
        """
        知识原子双路检索（精确 + 语义）

        返回 AtomResult 列表，每项包含：
          - id, type, category_id, trigger, content
          - confidence, verified, score, matched_by
        """
        payload: dict = {"query": query, "top_k": top_k, "task_error_keywords": task_error_keywords or []}
        if category_id:
            payload["category_id"] = category_id
        if hci_version:
            payload["hci_version"] = hci_version

        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
            try:
                resp = await client.post(
                    f"{self._atoms_prefix}/search",
                    json=payload,
                )
                resp.raise_for_status()
                data = resp.json()
                return data.get("atoms", [])
            except httpx.HTTPStatusError as exc:
                logger.warning(
                    event="kb_atoms_http_error",
                    message=f"KB atoms search returned HTTP {exc.response.status_code}",
                    query=query[:80],
                    status_code=exc.response.status_code,
                )
                return []
            except httpx.RequestError as exc:
                logger.warning(
                    event="kb_atoms_unavailable",
                    message=f"KB atoms service unreachable: {exc}",
                    query=query[:80],
                )
                return []
