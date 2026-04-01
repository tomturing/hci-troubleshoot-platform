"""
KnowledgeRetriever 单元测试

测试意图识别 + 三轨路由逻辑（SOP → KBD → 降级）
"""

import os
import sys
from unittest.mock import patch

import pytest

# 确保可以导入模块
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


class MockKBClient:
    """模拟 KB 客户端（新接口：classify_intent + route_by_category）"""

    def __init__(
        self,
        intent_result=None,
        route_result=None,
    ):
        self._intent_result = intent_result
        self._route_result = route_result

    async def classify_intent(self, query: str, top_n: int = 3):
        """模拟意图识别"""
        return self._intent_result

    async def route_by_category(self, category_code: str, query: str, top_k: int = 5):
        """模拟三轨路由"""
        return self._route_result


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
            assert meta["category_id"] is None
            assert meta["needs_review"] is True

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
    async def test_intent_classify_failed(self):
        """测试意图识别失败时进入机制推理模式"""
        from app.services.knowledge_retriever import KnowledgeRetriever

        # classify_intent 返回 None（失败）
        kb_client = MockKBClient(intent_result=None, route_result=None)

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

            # 验证降级到机制推理
            assert "机制推理模式" in prompt
            assert meta["fallback_level"] == "mechanism"

    @pytest.mark.asyncio
    async def test_sop_track_hit(self):
        """测试 SOP 轨道命中"""
        from app.services.knowledge_retriever import KnowledgeRetriever

        # 意图识别返回分类
        intent_result = {
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

        # 三轨路由返回 SOP 内容
        route_result = {
            "track": "sop",
            "category_id": "虚拟机-001",
            "results": [
                {
                    "id": 1,
                    "title": "VM 启动失败排障手册",
                    "content_md": "1. 检查主机资源\n2. 检查网络\n3. 检查存储",
                    "support_id": "sop-001",
                }
            ],
        }

        kb_client = MockKBClient(intent_result=intent_result, route_result=route_result)

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
            assert meta["category_id"] == "虚拟机-001"
            assert meta["category_score"] == 0.85

    @pytest.mark.asyncio
    async def test_kbd_track_hit(self):
        """测试 KBD 轨道命中"""
        from app.services.knowledge_retriever import KnowledgeRetriever

        # 意图识别返回分类
        intent_result = {
            "categories": [
                {
                    "category_id": 123,
                    "code": "虚拟机-002",
                    "name": "虚拟机状态异常",
                    "domain": "虚拟机",
                    "path_labels": ["虚拟机", "虚拟机状态"],
                    "score": 0.72,
                }
            ],
            "needs_review": False,
        }

        # 三轨路由返回 KBD 内容
        route_result = {
            "track": "kbd",
            "category_id": "虚拟机-002",
            "results": [
                {
                    "id": 1,
                    "title": "VM 无法启动案例",
                    "content_md": "客户反馈 VM 无法启动，检查发现是存储问题",
                    "support_id": "36156",
                    "category_id": "虚拟机-002",
                },
                {
                    "id": 2,
                    "title": "VM 启动超时案例",
                    "content_md": "类似问题，根因是磁盘空间不足",
                    "support_id": "36157",
                    "category_id": "虚拟机-002",
                },
            ],
        }

        kb_client = MockKBClient(intent_result=intent_result, route_result=route_result)

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

            # 验证 KBD 命中
            assert "历史案例参考" in prompt
            assert meta["fallback_level"] == "kb_case"
            assert meta["has_sop"] is False
            assert meta["kb_chunks_count"] == 2
            assert meta["category_id"] == "虚拟机-002"

    @pytest.mark.asyncio
    async def test_human_escalation_track(self):
        """测试人工兜底轨道"""
        from app.services.knowledge_retriever import KnowledgeRetriever

        # 意图识别返回分类
        intent_result = {
            "categories": [
                {
                    "category_id": 123,
                    "code": "虚拟机-003",
                    "name": "虚拟机资源不足",
                    "domain": "虚拟机",
                    "path_labels": ["虚拟机", "虚拟机资源"],
                    "score": 0.45,  # 低置信度
                }
            ],
            "needs_review": True,
        }

        # 三轨路由返回人工兜底
        route_result = {
            "track": "human_escalation",
            "category_id": "虚拟机-003",
            "results": [],
        }

        kb_client = MockKBClient(intent_result=intent_result, route_result=route_result)

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

            # 验证人工兜底（机制推理）
            assert "机制推理模式" in prompt
            assert meta["fallback_level"] == "human_escalation"
            assert meta["needs_review"] is True

    @pytest.mark.asyncio
    async def test_needs_review_flag(self):
        """测试 needs_review 标志正确传递"""
        from app.services.knowledge_retriever import KnowledgeRetriever

        # 意图识别返回低置信度分类
        intent_result = {
            "categories": [
                {
                    "category_id": 123,
                    "code": "虚拟机-001",
                    "name": "虚拟机创建失败",
                    "domain": "虚拟机",
                    "path_labels": ["虚拟机", "虚拟机创建"],
                    "score": 0.35,  # 低于阈值
                }
            ],
            "needs_review": True,  # 需要人工确认
        }

        route_result = {
            "track": "kbd",
            "category_id": "虚拟机-001",
            "results": [
                {
                    "id": 1,
                    "title": "测试案例",
                    "content_md": "测试内容",
                    "support_id": "12345",
                }
            ],
        }

        kb_client = MockKBClient(intent_result=intent_result, route_result=route_result)

        with patch("app.services.knowledge_retriever.settings") as mock_settings:
            mock_settings.KB_ENABLED = True
            mock_settings.KB_SEARCH_TOP_N = 5
            mock_settings.KB_CONTEXT_MAX_CHARS = 4000

            retriever = KnowledgeRetriever(kb_client=kb_client)
            prompt, meta = await retriever.retrieve(
                query="测试查询",
                case_id="case-001",
                diagnostic_stage="S0",
            )

            # 验证 needs_review 正确传递
            assert meta["needs_review"] is True
            assert meta["category_score"] == 0.35

    @pytest.mark.asyncio
    async def test_context_breakdown(self):
        """测试 context_breakdown 正确生成"""
        from app.services.knowledge_retriever import KnowledgeRetriever

        intent_result = {
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

        route_result = {
            "track": "kbd",
            "category_id": "虚拟机-001",
            "results": [
                {
                    "id": 1,
                    "title": "测试案例",
                    "content_md": "测试内容",
                    "support_id": "12345",
                }
            ],
        }

        kb_client = MockKBClient(intent_result=intent_result, route_result=route_result)

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
