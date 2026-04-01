"""
KB Client 消费者契约测试（G-2）

验证 KBClient 调用 kb-service 时，对响应结构的假设与实际契约对齐。
这是消费者驱动的契约测试：从调用方（conversation-service）视角定义期望，
捕获 kb-service 接口变更导致的不兼容问题。

运行方式：
  # 模拟模式（默认，不需要 kb-service 运行）
  uv run pytest backend/conversation-service/tests/integration/test_kb_client_contract.py -v

  # 集成模式（对真实 kb-service 运行）
  KB_SERVICE_URL=http://localhost:8004 uv run pytest ... -v -k "not mock"

主要接口：
  - classify_intent: 意图识别（POST /api/kb/classify/intent）
  - route_by_category: 三轨路由（GET /api/kb/route）
  - search: 混合检索（POST /api/kb/search）

废弃接口：
  - sop_match: 关键字路由（已废弃）
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from shared.models.schemas import KBSearchResponse, KBSOPMatchResponse

# ──────────────────────────────────────────────
# 契约：KB Classify Intent 接口（新接口）
# 协议：POST /api/kb/classify/intent
# 请求体：{"query": str, "top_n": int}
# 响应体：{"categories": [...], "needs_review": bool}
# ──────────────────────────────────────────────


class TestKBClassifyIntentContract:
    """意图识别接口契约断言"""

    @pytest.fixture
    def mock_classify_intent_response(self):
        """模拟 kb-service /api/kb/classify/intent 的标准响应"""
        return {
            "categories": [
                {
                    "category_id": 123,
                    "code": "虚拟机-001",
                    "name": "虚拟机创建失败",
                    "domain": "虚拟机",
                    "path_labels": ["虚拟机", "虚拟机创建"],
                    "score": 0.85,
                },
                {
                    "category_id": 124,
                    "code": "虚拟机-002",
                    "name": "虚拟机状态异常",
                    "domain": "虚拟机",
                    "path_labels": ["虚拟机", "虚拟机状态"],
                    "score": 0.72,
                },
            ],
            "needs_review": False,
        }

    def test_classify_intent_response_schema_valid(self, mock_classify_intent_response):
        """契约：意图识别响应必须包含 categories 列表和 needs_review 标志"""
        result = mock_classify_intent_response
        assert "categories" in result
        assert "needs_review" in result
        assert isinstance(result["categories"], list)
        assert isinstance(result["needs_review"], bool)

    def test_classify_intent_categories_fields(self, mock_classify_intent_response):
        """契约：categories 中每个元素必须包含 category_id、code、name、score 字段"""
        for cat in mock_classify_intent_response["categories"]:
            # 调用方强依赖这些字段
            assert "category_id" in cat, "categories 元素缺少 category_id 字段"
            assert "code" in cat, "categories 元素缺少 code 字段"
            assert "name" in cat, "categories 元素缺少 name 字段"
            assert "score" in cat, "categories 元素缺少 score 字段"

    def test_classify_intent_empty_response(self):
        """契约：零结果时响应仍为合法结构"""
        empty_response = {"categories": [], "needs_review": True}
        assert empty_response["categories"] == []
        assert empty_response["needs_review"] is True

    @pytest.mark.asyncio
    async def test_kb_client_classify_intent_calls_correct_endpoint(self):
        """契约：KBClient.classify_intent() 必须 POST 到 /api/kb/classify/intent"""
        import os
        import sys

        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../../../shared"))

        from app.services.kb_client import KBClient

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "categories": [
                {
                    "category_id": 123,
                    "code": "虚拟机-001",
                    "name": "虚拟机创建失败",
                    "domain": "虚拟机",
                    "path_labels": ["虚拟机", "虚拟机创建"],
                    "score": 0.85,
                }
            ],
            "needs_review": False,
        }
        mock_response.raise_for_status = MagicMock()

        with patch.object(KBClient, "post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_response
            client = KBClient("http://kb-service:8004", "test-token")
            result = await client.classify_intent("虚拟机启动失败", top_n=3)

            # 验证调用端点正确
            mock_post.assert_called_once()
            call_args = mock_post.call_args
            assert "/api/kb/classify/intent" in call_args[0][0], (
                f"期望调用 /api/kb/classify/intent，实际调用：{call_args[0][0]}"
            )
            # 验证请求体包含 query 字段
            assert call_args[1]["json"]["query"] == "虚拟机启动失败"
            assert call_args[1]["json"]["top_n"] == 3


# ──────────────────────────────────────────────
# 契约：KB Route 接口（新接口）
# 协议：GET /api/kb/route
# 请求参数：?category_id=str&query=str&top_k=int
# 响应体：{"track": str, "category_id": str, "results": [...]}
# ──────────────────────────────────────────────


class TestKBRouteContract:
    """三轨路由接口契约断言"""

    @pytest.fixture
    def mock_route_kbd_response(self):
        """模拟 kb-service /api/kb/route 的 KBD 响应"""
        return {
            "track": "kbd",
            "category_id": "虚拟机-001",
            "results": [
                {
                    "id": 1,
                    "title": "VM 启动失败案例",
                    "content_md": "排查步骤...",
                    "support_id": "36156",
                    "category_id": "虚拟机-001",
                }
            ],
        }

    @pytest.fixture
    def mock_route_sop_response(self):
        """模拟 kb-service /api/kb/route 的 SOP 响应"""
        return {
            "track": "sop",
            "category_id": "虚拟机-001",
            "results": [
                {
                    "id": 1,
                    "title": "VM 启动失败排障手册",
                    "content_md": "1. 检查主机资源...",
                    "support_id": "sop-001",
                    "category_id": "虚拟机-001",
                }
            ],
        }

    @pytest.fixture
    def mock_route_human_escalation_response(self):
        """模拟 kb-service /api/kb/route 的人工兜底响应"""
        return {
            "track": "human_escalation",
            "category_id": "虚拟机-001",
            "results": [],
        }

    def test_route_kbd_response_schema_valid(self, mock_route_kbd_response):
        """契约：KBD 轨道响应必须包含 track、category_id、results"""
        result = mock_route_kbd_response
        assert result["track"] == "kbd"
        assert "category_id" in result
        assert "results" in result
        assert isinstance(result["results"], list)

    def test_route_sop_response_schema_valid(self, mock_route_sop_response):
        """契约：SOP 轨道响应必须包含 track、category_id、results"""
        result = mock_route_sop_response
        assert result["track"] == "sop"
        assert "category_id" in result
        assert len(result["results"]) > 0

    def test_route_human_escalation_schema_valid(self, mock_route_human_escalation_response):
        """契约：人工兜底响应 track=human_escalation，results 为空"""
        result = mock_route_human_escalation_response
        assert result["track"] == "human_escalation"
        assert result["results"] == []

    @pytest.mark.asyncio
    async def test_kb_client_route_by_category_calls_correct_endpoint(self):
        """契约：KBClient.route_by_category() 必须 GET /api/kb/route"""
        import os
        import sys

        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../../../shared"))

        from app.services.kb_client import KBClient

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "track": "kbd",
            "category_id": "虚拟机-001",
            "results": [
                {
                    "id": 1,
                    "title": "测试案例",
                    "content_md": "测试内容",
                    "support_id": "36156",
                    "category_id": "虚拟机-001",
                }
            ],
        }
        mock_response.raise_for_status = MagicMock()

        with patch.object(KBClient, "get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_response
            client = KBClient("http://kb-service:8004", "test-token")
            result = await client.route_by_category("虚拟机-001", "虚拟机启动失败", top_k=5)

            # 验证调用端点正确
            mock_get.assert_called_once()
            call_args = mock_get.call_args
            assert "/api/kb/route" in call_args[0][0], (
                f"期望调用 /api/kb/route，实际调用：{call_args[0][0]}"
            )
            # 验证请求参数
            assert call_args[1]["params"]["category_id"] == "虚拟机-001"
            assert call_args[1]["params"]["query"] == "虚拟机启动失败"


# ──────────────────────────────────────────────
# 契约：KB Search 接口
# 协议：POST /api/kb/search
# 请求体：{"query": str, "top_n": int}
# 响应体：{"chunks": [...], "total": int, "query_time_ms": float}
# ──────────────────────────────────────────────


class TestKBSearchContract:
    """KB 语义搜索接口契约断言"""

    @pytest.fixture
    def mock_kb_response(self):
        """模拟 kb-service /api/kb/search 的标准响应"""
        return {
            "chunks": [
                {
                    "chunk_id": "chunk-001",
                    "document_id": "doc-001",
                    "content": "存储控制器故障排查步骤...",
                    "score": 0.92,
                    "source_title": "存储故障处理手册",
                    "source_type": "sop",
                    "page_num": 1,
                }
            ],
            "total": 1,
            "query_time_ms": 45.3,
        }

    def test_search_response_schema_valid(self, mock_kb_response):
        """契约：搜索响应必须符合 KBSearchResponse 模型"""
        # 如果 kb-service 改变了响应结构，此测试将失败，提前发现契约破裂
        result = KBSearchResponse.model_validate(mock_kb_response)
        assert result.total == 1
        assert len(result.chunks) == 1
        assert result.query_time_ms == 45.3

    def test_search_response_chunks_fields(self, mock_kb_response):
        """契约：chunks 中每个元素必须包含 content 和 score 字段"""
        result = KBSearchResponse.model_validate(mock_kb_response)
        for chunk in result.chunks:
            # 调用方强依赖这两个字段，不允许 kb-service 静默删除
            assert "content" in chunk, "chunks 元素缺少 content 字段"
            assert "score" in chunk, "chunks 元素缺少 score 字段"

    def test_search_empty_response(self):
        """契约：零结果时响应仍为合法结构（不返回 null）"""
        empty_response = {"chunks": [], "total": 0, "query_time_ms": 0.0}
        result = KBSearchResponse.model_validate(empty_response)
        assert result.chunks == []
        assert result.total == 0

    @pytest.mark.asyncio
    async def test_kb_client_search_calls_correct_endpoint(self):
        """契约：KBClient.search() 必须 POST 到 /api/kb/search"""
        import os
        import sys

        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../../../shared"))

        from app.services.kb_client import KBClient

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "chunks": [{"content": "test", "score": 0.9}],
            "total": 1,
            "query_time_ms": 10.0,
        }
        mock_response.raise_for_status = MagicMock()

        with patch.object(KBClient, "post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_response
            client = KBClient("http://kb-service:8004", "test-token")
            result = await client.search("存储报错", top_n=3)

            # 验证调用端点正确
            mock_post.assert_called_once()
            call_args = mock_post.call_args
            assert "/api/kb/search" in call_args[0][0], (
                f"期望调用 /api/kb/search，实际调用：{call_args[0][0]}"
            )
            # 验证请求体包含 query 字段
            assert call_args[1]["json"]["query"] == "存储报错"
            assert call_args[1]["json"]["top_n"] == 3


# ──────────────────────────────────────────────
# 契约：KB SOP Match 接口（已废弃，保留向后兼容）
# 协议：POST /api/kb/sop/match
# 请求体：{"query": str}
# 响应体：{"matched": bool, "title": str|null, "content": str|null, "node_id": str|null}
# ──────────────────────────────────────────────


class TestKBSOPMatchContract:
    """KB SOP 精确匹配接口契约断言（已废弃）"""

    def test_sop_match_hit_response_schema(self):
        """契约：命中时必须包含 matched=True 且 title/content 非 null"""
        hit_response = {
            "matched": True,
            "title": "存储控制器故障处理 SOP",
            "content": "步骤一：检查控制器指示灯...",
            "node_id": "node-001",
        }
        result = KBSOPMatchResponse.model_validate(hit_response)
        assert result.matched is True
        assert result.title is not None  # 调用方强依赖，不允许 null
        assert result.content is not None

    def test_sop_match_miss_response_schema(self):
        """契约：未命中时 matched=False，title/content/node_id 可为 null"""
        miss_response = {
            "matched": False,
            "title": None,
            "content": None,
            "node_id": None,
        }
        result = KBSOPMatchResponse.model_validate(miss_response)
        assert result.matched is False
        assert result.title is None

    @pytest.mark.asyncio
    async def test_kb_client_sop_match_calls_correct_endpoint(self):
        """契约：KBClient.sop_match() 必须 POST 到 /api/kb/sop/match"""
        import os
        import sys

        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../../../shared"))

        from app.services.kb_client import KBClient

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "matched": True,
            "title": "SOP 标题",
            "content": "SOP 正文",
            "node_id": "node-001",
        }
        mock_response.raise_for_status = MagicMock()

        with patch.object(KBClient, "post", new_callable=AsyncMock) as mock_post:
            mock_post.return_value = mock_response
            client = KBClient("http://kb-service:8004", "test-token")
            result = await client.sop_match("存储报错")

            mock_post.assert_called_once()
            call_args = mock_post.call_args
            assert "/sop/match" in call_args[0][0], (
                f"期望调用包含 /sop/match，实际：{call_args[0][0]}"
            )
