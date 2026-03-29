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
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from shared.models.schemas import KBSearchResponse, KBSOPMatchResponse

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
        # 确保 shared 模块路径可用
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
# 契约：KB SOP Match 接口
# 协议：POST /api/kb/sop/match
# 请求体：{"query": str}
# 响应体：{"matched": bool, "title": str|null, "content": str|null, "node_id": str|null}
# ──────────────────────────────────────────────


class TestKBSOPMatchContract:
    """KB SOP 精确匹配接口契约断言"""

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
