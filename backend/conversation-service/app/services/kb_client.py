"""
KB Client - 知识库服务客户端
负责调用 kb-service 进行意图识别、混合检索和 SOP 匹配

主要接口：
  - classify_intent: 意图识别（返回 category_id）
  - search: 混合检索（BM25 + 向量 RRF 融合）
  - route_by_category: 三轨路由（根据 category_id 获取知识内容）

废弃接口：
  - sop_match: 关键字路由（已废弃，改用 classify_intent + route_by_category）
"""

import httpx
from shared.utils.internal_http import InternalHTTPClient
from shared.utils.logger import get_logger

logger = get_logger("conversation-kb-client")

# 超时配置（KB 检索通常需要向量计算，给予充足时间）
_REQUEST_TIMEOUT = 10.0
# 分类列表获取超时（较短，因为只是简单查询）
_CATEGORY_TIMEOUT = 5.0


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

    async def classify_intent(
        self,
        query: str,
        top_n: int = 3,
    ) -> dict | None:
        """
        意图识别（调用 kb-service POST /api/kb/classify/intent）

        返回分类候选列表，包含：
          - categories: [{ category_id, code, name, domain, path_labels, score }]
          - needs_review: bool

        用途：
          - 对话开始时进行意图识别，获取 category_id
          - category_id 用于后续调用 route_by_category 获取知识内容
        """
        try:
            resp = await self.post(
                f"{self._api_prefix}/classify/intent",
                json={"query": query, "top_n": top_n},
            )
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as exc:
            logger.warning(
                event="kb_classify_intent_http_error",
                message=f"KB classify intent returned HTTP {exc.response.status_code}",
                query=query[:80],
                status_code=exc.response.status_code,
            )
            return None
        except httpx.RequestError as exc:
            logger.warning(
                event="kb_classify_intent_unavailable",
                message=f"KB service unreachable: {exc}",
                query=query[:80],
            )
            return None

    async def route_by_category(
        self,
        category_code: str,
        query: str,
        top_k: int = 5,
    ) -> dict | None:
        """
        三轨路由（根据 category_id 获取知识内容）

        调用 kb-service GET /api/kb/route?category_id=X&query=...

        返回：
          - track: "sop" | "kbd" | "human_escalation"
          - category_id: 分类编码
          - results: [{ id, title, content_md, support_id, category_id }]
        """
        try:
            resp = await self.get(
                f"{self._api_prefix}/route",
                params={"category_id": category_code, "query": query, "top_k": top_k},
            )
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as exc:
            logger.warning(
                event="kb_route_http_error",
                message=f"KB route returned HTTP {exc.response.status_code}",
                category_code=category_code,
                query=query[:80],
                status_code=exc.response.status_code,
            )
            return None
        except httpx.RequestError as exc:
            logger.warning(
                event="kb_route_unavailable",
                message=f"KB service unreachable: {exc}",
                category_code=category_code,
                query=query[:80],
            )
            return None

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

        [已废弃] 改用 classify_intent + route_by_category 进行意图识别和知识检索

        返回命中节点的完整内容，未命中返回 None：
          - node_id, title, content, category, keywords

        注意：此方法基于 kb_sop_node 关键字路由，是"已知最差的触发机制"。
        请改用新的意图识别流程：
          1. classify_intent(query) → 获取 category_id
          2. route_by_category(category_code, query) → 获取知识内容
        """
        logger.warning(
            event="deprecated_sop_match_called",
            message="sop_match 已废弃，请改用 classify_intent + route_by_category",
            query=query[:80],
        )
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

    async def get_categories_grouped(self) -> dict[str, list[dict]]:
        """
        获取分类列表（按域分组）

        用于 S0 意图识别阶段，将 198 个分类注入 Prompt。
        返回格式：
        {
            "虚拟机": [{"id": "虚拟机-001", "label": "虚拟机创建失败"}, ...],
            "网络": [...],
            "存储": [...],
            "硬件": [...],
            "平台": [...],
        }
        """
        try:
            async with httpx.AsyncClient(timeout=_CATEGORY_TIMEOUT) as client:
                resp = await client.get(
                    f"{self._api_prefix}/categories/grouped",
                    headers=self._headers,
                )
                resp.raise_for_status()
                data = resp.json()
                return data.get("categories_by_domain", {})
        except httpx.HTTPStatusError as exc:
            logger.warning(
                event="kb_categories_http_error",
                message=f"KB categories returned HTTP {exc.response.status_code}",
                status_code=exc.response.status_code,
            )
            return {}
        except httpx.RequestError as exc:
            logger.warning(
                event="kb_categories_unavailable",
                message=f"KB service unreachable: {exc}",
            )
            return {}

    async def increment_category_hit(self, code: str) -> int:
        """
        分类命中计数 +1

        当 LLM 在 S0 阶段确认故障分类时调用，用于分析热门/冷门分类。

        Args:
            code: 分类编码，如 "虚拟机-003"

        Returns:
            更新后的 hit_count 值，失败返回 -1
        """
        try:
            async with httpx.AsyncClient(timeout=_CATEGORY_TIMEOUT) as client:
                resp = await client.post(
                    f"{self._api_prefix}/categories/{code}/hit",
                    headers=self._headers,
                )
                resp.raise_for_status()
                data = resp.json()
                hit_count = data.get("hit_count", -1)
                logger.info(
                    event="category_hit_incremented",
                    message=f"分类 {code} 命中计数已更新为 {hit_count}",
                    code=code,
                    hit_count=hit_count,
                )
                return hit_count
        except httpx.HTTPStatusError as exc:
            logger.warning(
                event="kb_category_hit_http_error",
                message=f"KB category hit returned HTTP {exc.response.status_code}",
                code=code,
                status_code=exc.response.status_code,
            )
            return -1
        except httpx.RequestError as exc:
            logger.warning(
                event="kb_category_hit_unavailable",
                message=f"KB service unreachable: {exc}",
                code=code,
            )
            return -1
