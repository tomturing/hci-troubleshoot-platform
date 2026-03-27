"""
KnowledgeRetriever 单元测试

测试三轨知识检索逻辑（SOP → KB → 降级）
"""

import os
import sys
from unittest.mock import patch

import pytest

# 确保可以导入模块
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


class MockKBClient:
    """模拟 KB 客户端"""

    def __init__(self, sop_result=None, search_result=None):
        self._sop_result = sop_result
        self._search_result = search_result or []

    async def sop_match(self, query: str):
        return self._sop_result

    async def search(self, query: str, top_n: int = 5):
        return self._search_result


class TestKnowledgeRetriever:
    """KnowledgeRetriever 测试用例"""

    @pytest.mark.asyncio
    async def test_kb_not_enabled(self):
        """测试 KB 未启用时进入机制推理模式"""
        from app.services.knowledge_retriever import KnowledgeRetriever

        with patch("app.services.knowledge_retriever.settings") as mock_settings:
            mock_settings.KB_ENABLED = False

            retriever = KnowledgeRetriever(kb_client=None)
            prompt, meta = await retriever.retrieve(
                query="测试查询",
                case_id="case-001",
                diagnostic_stage="S0",
            )

            # 验证进入机制推理模式
            assert "机制推理模式" in prompt
            assert meta["fallback_level"] == "mechanism"
            assert meta["has_sop"] is False
            assert meta["kb_chunks_count"] == 0
            assert meta["kb_top_score"] is None

    @pytest.mark.asyncio
    async def test_kb_client_none(self):
        """测试 kb_client 为 None 时进入机制推理模式"""
        from app.services.knowledge_retriever import KnowledgeRetriever

        with patch("app.services.knowledge_retriever.settings") as mock_settings:
            mock_settings.KB_ENABLED = True

            retriever = KnowledgeRetriever(kb_client=None)
            prompt, meta = await retriever.retrieve(
                query="测试查询",
                case_id="case-001",
                diagnostic_stage="S0",
            )

            assert "机制推理模式" in prompt
            assert meta["fallback_level"] == "mechanism"

    @pytest.mark.asyncio
    async def test_sop_hit(self):
        """测试 SOP 命中时使用 SOP 轨道"""
        from app.services.knowledge_retriever import KnowledgeRetriever

        sop_node = {
            "node_id": "sop-001",
            "title": "VM 启动失败排障手册",
            "content": "1. 检查主机资源\n2. 检查网络\n3. 检查存储",
            "category": "vm",
        }
        kb_client = MockKBClient(sop_result=sop_node, search_result=[])

        with patch("app.services.knowledge_retriever.settings") as mock_settings:
            mock_settings.KB_ENABLED = True
            mock_settings.KB_SEARCH_TOP_N = 5
            mock_settings.KB_CONTEXT_MAX_CHARS = 4000

            retriever = KnowledgeRetriever(kb_client=kb_client)
            prompt, meta = await retriever.retrieve(
                query="虚拟机启动失败",
                case_id="case-001",
                diagnostic_stage="S1",
            )

            # 验证 SOP 命中
            assert "SOP 排障流程" in prompt
            assert "VM 启动失败排障手册" in prompt
            assert meta["fallback_level"] == "sop"
            assert meta["has_sop"] is True
            assert meta["kb_chunks_count"] == 0

    @pytest.mark.asyncio
    async def test_kb_case_hit(self):
        """测试 KB 案例命中时使用案例轨道"""
        from app.services.knowledge_retriever import KnowledgeRetriever

        kb_chunks = [
            {
                "chunk_id": "chunk-1",
                "content": "客户反馈 VM 无法启动，检查发现是存储问题",
                "score": 0.95,
                "source_title": "案例库 A",
                "case_id": "case-100",
            },
            {
                "chunk_id": "chunk-2",
                "content": "类似问题，根因是磁盘空间不足",
                "score": 0.88,
                "source_title": "案例库 B",
            },
        ]
        kb_client = MockKBClient(sop_result=None, search_result=kb_chunks)

        with patch("app.services.knowledge_retriever.settings") as mock_settings:
            mock_settings.KB_ENABLED = True
            mock_settings.KB_SEARCH_TOP_N = 5
            mock_settings.KB_CONTEXT_MAX_CHARS = 4000

            retriever = KnowledgeRetriever(kb_client=kb_client)
            prompt, meta = await retriever.retrieve(
                query="VM 启动问题",
                case_id="case-001",
                diagnostic_stage="S2",
            )

            # 验证 KB 案例命中
            assert "历史案例参考" in prompt
            assert meta["fallback_level"] == "kb_case"
            assert meta["has_sop"] is False
            assert meta["kb_chunks_count"] == 2
            assert meta["kb_top_score"] == 0.95

    @pytest.mark.asyncio
    async def test_no_hit_fallback(self):
        """测试双轨均未命中时进入机制推理模式"""
        from app.services.knowledge_retriever import KnowledgeRetriever

        kb_client = MockKBClient(sop_result=None, search_result=[])

        with patch("app.services.knowledge_retriever.settings") as mock_settings:
            mock_settings.KB_ENABLED = True
            mock_settings.KB_SEARCH_TOP_N = 5
            mock_settings.KB_CONTEXT_MAX_CHARS = 4000

            retriever = KnowledgeRetriever(kb_client=kb_client)
            prompt, meta = await retriever.retrieve(
                query="完全陌生的故障",
                case_id="case-001",
                diagnostic_stage="S0",
            )

            # 验证降级到机制推理
            assert "机制推理模式" in prompt
            assert meta["fallback_level"] == "mechanism"
            assert meta["has_sop"] is False
            assert meta["kb_chunks_count"] == 0

    @pytest.mark.asyncio
    async def test_error_handling(self):
        """测试异常处理不影响主流程"""
        from app.services.knowledge_retriever import KnowledgeRetriever

        class ErrorKBClient:
            async def sop_match(self, query: str):
                raise Exception("SOP 服务异常")

            async def search(self, query: str, top_n: int = 5):
                raise Exception("搜索服务异常")

        kb_client = ErrorKBClient()

        with patch("app.services.knowledge_retriever.settings") as mock_settings:
            mock_settings.KB_ENABLED = True
            mock_settings.KB_SEARCH_TOP_N = 5
            mock_settings.KB_CONTEXT_MAX_CHARS = 4000

            retriever = KnowledgeRetriever(kb_client=kb_client)
            # 异常不应抛出，而是进入机制推理模式
            prompt, meta = await retriever.retrieve(
                query="测试查询",
                case_id="case-001",
                diagnostic_stage="S0",
            )

            assert "机制推理模式" in prompt

    @pytest.mark.asyncio
    async def test_context_breakdown(self):
        """测试 context_breakdown 正确生成"""
        from app.services.knowledge_retriever import KnowledgeRetriever

        kb_chunks = [
            {
                "chunk_id": "chunk-1",
                "content": "测试内容",
                "score": 0.9,
            }
        ]
        kb_client = MockKBClient(sop_result=None, search_result=kb_chunks)

        with patch("app.services.knowledge_retriever.settings") as mock_settings:
            mock_settings.KB_ENABLED = True
            mock_settings.KB_SEARCH_TOP_N = 5
            mock_settings.KB_CONTEXT_MAX_CHARS = 4000

            retriever = KnowledgeRetriever(kb_client=kb_client)
            prompt, meta = await retriever.retrieve(
                query="测试",
                case_id="case-001",
                diagnostic_stage="S0",
            )

            # 验证 context_breakdown 存在
            assert "context_breakdown" in meta
            assert "total_chars" in meta
            assert "total_token_est" in meta
            assert meta["total_chars"] > 0
            assert len(meta["context_breakdown"]) > 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
