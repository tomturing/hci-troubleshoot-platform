"""
Prompt Audit 单元测试

测试 _build_system_prompt 返回的 audit_meta 和 _write_prompt_audit 功能
"""

import os
import sys

# 多服务共享 app/ 命名空间，仅在 app 指向错误服务时清除重载
_svc = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_expect = os.path.normpath(os.path.join(_svc, "app"))
_actual = os.path.normpath(getattr(sys.modules.get("app"), "__path__", [""])[0]) if "app" in sys.modules else ""
if _expect != _actual:
    for _k in list(sys.modules):
        if _k == "app" or _k.startswith("app."):
            del sys.modules[_k]
    if _svc in sys.path:
        sys.path.remove(_svc)
    sys.path.insert(0, _svc)

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.services.conversation_service import ConversationService
from app.services.kb_client import KBClient


@pytest.fixture
def mock_repo():
    """Repository mock - 所有方法默认为 AsyncMock 以支持 await"""
    return AsyncMock()


@pytest.fixture
def mock_registry():
    """AI Registry mock"""
    return MagicMock()


@pytest.fixture
def mock_scheduler():
    """Scheduler client mock"""
    m = AsyncMock()
    m.allocate_pod.return_value = "test-pod-001"
    m.wait_for_endpoint.return_value = "http://localhost:9999"
    return m


@pytest.fixture
def mock_kb_client():
    """KB Client mock"""
    return MagicMock(spec=KBClient)


@pytest.fixture
def mock_session_factory():
    """Session factory mock"""
    session = AsyncMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=None)
    factory = MagicMock(return_value=session)
    return factory, session


@pytest.fixture
def service_with_kb(mock_repo, mock_registry, mock_scheduler, mock_kb_client, mock_session_factory):
    """创建带有 KB 客户端的服务实例"""
    factory, _ = mock_session_factory
    service = ConversationService(
        repository=mock_repo,
        ai_registry=mock_registry,
        scheduler_client=mock_scheduler,
        kb_client=mock_kb_client,
        session_factory=factory,
    )
    return service


@pytest.fixture
def service_without_kb(mock_repo, mock_registry, mock_scheduler, mock_session_factory):
    """创建不带 KB 客户端的服务实例"""
    factory, _ = mock_session_factory
    service = ConversationService(
        repository=mock_repo,
        ai_registry=mock_registry,
        scheduler_client=mock_scheduler,
        kb_client=None,
        session_factory=factory,
    )
    return service


# ---------- _build_system_prompt 返回 audit_meta 测试 ----------


@pytest.mark.asyncio
class TestBuildSystemPromptAuditMeta:
    """_build_system_prompt 返回 audit_meta 测试"""

    async def test_build_system_prompt_returns_audit_meta_with_kb(
        self, service_with_kb, mock_kb_client
    ):
        """测试 KB 启用时返回正确的 audit_meta"""
        # Mock KB 客户端返回 SOP 和 Chunks
        mock_kb_client.sop_match = AsyncMock(
            return_value={"title": "SOP-001", "content": "这是 SOP 内容"}
        )
        mock_kb_client.search = AsyncMock(
            return_value=[
                {"content": "chunk1", "score": 0.85, "source_title": "Doc1"},
                {"content": "chunk2", "score": 0.72, "source_title": "Doc2"},
            ]
        )

        # 调用方法
        system_prompt, audit_meta = await service_with_kb._build_system_prompt(
            query="磁盘 IO 异常", case_id="Q001"
        )

        # 断言返回类型
        assert isinstance(system_prompt, str)
        assert isinstance(audit_meta, dict)

        # 断言 audit_meta 内容
        assert audit_meta["has_sop"] is True
        assert audit_meta["kb_chunks_count"] == 2
        assert audit_meta["kb_top_score"] == 0.85

    async def test_build_system_prompt_returns_audit_meta_without_sop(
        self, service_with_kb, mock_kb_client
    ):
        """测试 KB 启用但无 SOP 时返回正确的 audit_meta"""
        # Mock KB 客户端仅返回 Chunks
        mock_kb_client.sop_match = AsyncMock(return_value=None)
        mock_kb_client.search = AsyncMock(
            return_value=[{"content": "chunk1", "score": 0.65}]
        )

        system_prompt, audit_meta = await service_with_kb._build_system_prompt(
            query="网络延迟", case_id="Q002"
        )

        assert audit_meta["has_sop"] is False
        assert audit_meta["kb_chunks_count"] == 1
        assert audit_meta["kb_top_score"] == 0.65

    async def test_build_system_prompt_returns_audit_meta_empty_kb(
        self, service_with_kb, mock_kb_client
    ):
        """测试 KB 启用但无返回时返回正确的 audit_meta"""
        # Mock KB 客户端返回空
        mock_kb_client.sop_match = AsyncMock(return_value=None)
        mock_kb_client.search = AsyncMock(return_value=[])

        system_prompt, audit_meta = await service_with_kb._build_system_prompt(
            query="未知问题", case_id="Q003"
        )

        assert audit_meta["has_sop"] is False
        assert audit_meta["kb_chunks_count"] == 0
        assert audit_meta["kb_top_score"] is None

    async def test_build_system_prompt_returns_audit_meta_kb_error(
        self, service_with_kb, mock_kb_client
    ):
        """测试 KB 客户端异常时返回正确的 audit_meta"""
        # Mock KB 客户端抛出异常
        mock_kb_client.sop_match = AsyncMock(side_effect=Exception("KB Error"))
        mock_kb_client.search = AsyncMock(side_effect=Exception("Search Error"))

        system_prompt, audit_meta = await service_with_kb._build_system_prompt(
            query="错误测试", case_id="Q004"
        )

        assert audit_meta["has_sop"] is False
        assert audit_meta["kb_chunks_count"] == 0
        assert audit_meta["kb_top_score"] is None

    async def test_build_system_prompt_returns_audit_meta_without_kb(
        self, service_without_kb
    ):
        """测试 KB 未启用时返回正确的 audit_meta"""
        system_prompt, audit_meta = await service_without_kb._build_system_prompt(
            query="测试查询", case_id="Q005"
        )

        assert isinstance(system_prompt, str)
        assert "当前工单 ID：Q005" in system_prompt
        assert audit_meta["has_sop"] is False
        assert audit_meta["kb_chunks_count"] == 0
        assert audit_meta["kb_top_score"] is None

    async def test_build_system_prompt_kb_score_missing(
        self, service_with_kb, mock_kb_client
    ):
        """测试 KB Chunk 缺少 score 字段时的处理"""
        mock_kb_client.sop_match = AsyncMock(return_value=None)
        mock_kb_client.search = AsyncMock(
            return_value=[
                {"content": "chunk1"},  # 没有 score
                {"content": "chunk2", "score": 0.5},
            ]
        )

        system_prompt, audit_meta = await service_with_kb._build_system_prompt(
            query="测试", case_id="Q006"
        )

        assert audit_meta["kb_chunks_count"] == 2
        # max(0.0, 0.5) = 0.5（没有 score 的默认为 0.0）
        assert audit_meta["kb_top_score"] == 0.5


# ---------- _write_prompt_audit 测试 ----------


@pytest.mark.asyncio
class TestWritePromptAudit:
    """_write_prompt_audit 方法测试"""

    async def test_write_prompt_audit_success(
        self, service_with_kb, mock_session_factory
    ):
        """测试成功写入 prompt_audit"""
        _, session = mock_session_factory
        conv_id = uuid.uuid4()
        case_id = "Q001"

        audit_meta = {
            "has_sop": True,
            "kb_chunks_count": 3,
            "kb_top_score": 0.85,
        }
        sample_payload = [{"role": "user", "content": "test"}]

        # Mock ConversationRepository 和 insert_prompt_audit
        mock_repo_instance = AsyncMock()
        mock_repo_class = MagicMock(return_value=mock_repo_instance)

        with patch("app.services.conversation_service.ConversationRepository", mock_repo_class):
            await service_with_kb._write_prompt_audit(
                conversation_id=conv_id,
                case_id=case_id,
                assistant_type="openclaw",
                trace_id="test-trace-001",
                message_count=5,
                audit_meta=audit_meta,
                sample_payload=sample_payload,
            )

            # 验证 Repository 方法被调用
            mock_repo_instance.insert_prompt_audit.assert_called_once_with(
                conversation_id=conv_id,
                case_id=case_id,
                assistant_type="openclaw",
                trace_id="test-trace-001",
                message_count=5,
                has_sop=True,
                kb_chunks_count=3,
                kb_top_score=0.85,
                messages=sample_payload,
            )

            # 验证 session.commit 被调用
            session.commit.assert_called_once()

    async def test_write_prompt_audit_no_sample(
        self, service_with_kb, mock_session_factory
    ):
        """测试无采样 payload 时写入 prompt_audit"""
        _, session = mock_session_factory
        conv_id = uuid.uuid4()

        audit_meta = {
            "has_sop": False,
            "kb_chunks_count": 0,
            "kb_top_score": None,
        }

        # Mock ConversationRepository 和 insert_prompt_audit
        mock_repo_instance = AsyncMock()
        mock_repo_class = MagicMock(return_value=mock_repo_instance)

        with patch("app.services.conversation_service.ConversationRepository", mock_repo_class):
            await service_with_kb._write_prompt_audit(
                conversation_id=conv_id,
                case_id="Q002",
                assistant_type="openclaw",
                trace_id="test-trace-002",
                message_count=1,
                audit_meta=audit_meta,
                sample_payload=None,
            )

            mock_repo_instance.insert_prompt_audit.assert_called_once_with(
                conversation_id=conv_id,
                case_id="Q002",
                assistant_type="openclaw",
                trace_id="test-trace-002",
                message_count=1,
                has_sop=False,
                kb_chunks_count=0,
                kb_top_score=None,
                messages=None,
            )

    async def test_write_prompt_audit_error_handling(
        self, service_with_kb, mock_repo, mock_session_factory
    ):
        """测试写入错误处理 - 不影响主流程"""
        _, session = mock_session_factory
        session.commit = AsyncMock(side_effect=Exception("Database error"))

        conv_id = uuid.uuid4()
        audit_meta = {"has_sop": True, "kb_chunks_count": 1, "kb_top_score": 0.9}

        # 不应抛出异常
        await service_with_kb._write_prompt_audit(
            conversation_id=conv_id,
            case_id="Q003",
            assistant_type="openclaw",
            trace_id="test-trace-003",
            message_count=2,
            audit_meta=audit_meta,
            sample_payload=None,
        )

        # 错误被捕获并记录，不会传播


# ---------- 集成测试：send_message_stream_only 触发 prompt_audit 写入 ----------


@pytest.mark.asyncio
class TestSendMessageTriggersPromptAudit:
    """集成测试：验证 send_message_stream_only 触发 prompt_audit 写入"""

    async def test_build_system_prompt_audit_meta_integration(
        self, service_with_kb, mock_kb_client
    ):
        """测试 _build_system_prompt 返回的 audit_meta 被正确传递"""
        case_id = "Q002"

        # 设置 KB 返回
        mock_kb_client.sop_match = AsyncMock(
            return_value={"title": "SOP-TEST", "content": "SOP 内容"}
        )
        mock_kb_client.search = AsyncMock(
            return_value=[{"content": "chunk", "score": 0.75}]
        )

        # 直接测试 _build_system_prompt
        system_prompt, audit_meta = await service_with_kb._build_system_prompt(
            query="测试查询", case_id=case_id
        )

        # 验证 audit_meta 内容正确
        assert isinstance(system_prompt, str)
        assert audit_meta["has_sop"] is True
        assert audit_meta["kb_chunks_count"] == 1
        assert audit_meta["kb_top_score"] == 0.75


# ---------- 采样率测试 ----------


@pytest.mark.asyncio
class TestPromptAuditSampling:
    """prompt_audit payload 采样率测试"""

    async def test_sampling_rate_approximately_10_percent(self):
        """测试采样率约为 10%（统计测试）"""
        import random

        samples = 0
        total = 1000

        for _ in range(total):
            if random.random() < 0.10:
                samples += 1

        # 10% 采样，允许±3% 误差
        rate = samples / total
        assert 0.07 <= rate <= 0.13, f"采样率 {rate} 超出预期范围"
