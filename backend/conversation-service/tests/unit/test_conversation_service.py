"""
Conversation Service 单元测试 (v2.0 — 多类型AI助手架构)

测试 ConversationService(repository, ai_registry, scheduler_client) 的:
- create_conversation(case_id, assistant_type)
- send_message_stream_only(conversation_id, case_id, content, assistant_type)
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

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from app.models.conversation import Conversation
from app.models.message import MessageRole
from app.services.conversation_service import ConversationService, jaccard_similarity


@pytest.fixture
def mock_repo():
    """Repository mock - 所有方法默认为 AsyncMock 以支持 await"""
    return AsyncMock()


@pytest.fixture
def mock_registry():
    return MagicMock()


@pytest.fixture
def mock_scheduler():
    """Scheduler client mock - 配置常见返回值避免 JSON 序列化问题"""
    m = AsyncMock()
    m.allocate_pod.return_value = "test-pod-001"
    m.wait_for_endpoint.return_value = "http://localhost:9999"
    return m


@pytest.fixture
def service(mock_repo, mock_registry, mock_scheduler):
    return ConversationService(mock_repo, mock_registry, mock_scheduler)


# ---------- create_conversation ----------


@pytest.mark.asyncio
class TestCreateConversation:
    """create_conversation 测试"""

    async def test_create_conversation_success(self, service, mock_repo):
        """测试成功创建对话"""
        expected = Conversation(
            conversation_id=uuid.uuid4(),
            case_id="Q2024010100001",
            assistant_type="openclaw",
        )
        mock_repo.create_conversation = AsyncMock(return_value=expected)

        result = await service.create_conversation(
            case_id="Q2024010100001",
            assistant_type="openclaw",
        )

        assert result == expected
        mock_repo.create_conversation.assert_called_once()
        call_kwargs = mock_repo.create_conversation.call_args.kwargs
        assert call_kwargs["case_id"] == "Q2024010100001"
        assert call_kwargs["assistant_type"] == "openclaw"

    async def test_create_conversation_default_type(self, service, mock_repo):
        """测试默认 assistant_type 为 openclaw"""
        mock_repo.create_conversation = AsyncMock(return_value=MagicMock(spec=Conversation))
        await service.create_conversation(case_id="Q2024010100001")
        call_kwargs = mock_repo.create_conversation.call_args.kwargs
        assert call_kwargs["assistant_type"] == "openclaw"


# ---------- send_message_stream_only ----------


@pytest.mark.asyncio
class TestSendMessageStreamOnly:
    """send_message_stream_only 流式消息测试"""

    async def test_stream_returns_chunks(self, service, mock_repo, mock_registry):
        """测试流式返回 AI 回复"""
        conv_id = uuid.uuid4()
        case_id = "Q2024010100001"
        content = "你好"

        # Mock 依赖
        mock_repo.add_message = AsyncMock()
        mock_repo.get_messages = AsyncMock(return_value=[])

        # Mock AI 客户端流
        async def fake_stream(*args, **kwargs):
            for chunk in ["你好", "，我是", "AI"]:
                yield chunk

        mock_client = MagicMock()
        mock_client.chat_completion_stream = fake_stream
        mock_registry.get_client.return_value = mock_client

        chunks = []
        async for chunk in service.send_message_stream_only(
            conversation_id=conv_id,
            case_id=case_id,
            content=content,
            assistant_type="openclaw",
        ):
            chunks.append(chunk)

        assert "".join(chunks) == "你好，我是AI"

        # 验证用户消息已保存
        mock_repo.add_message.assert_any_call(
            conversation_id=conv_id,
            case_id=case_id,
            role=MessageRole.user,
            content=content,
            trace_id=mock_repo.add_message.call_args_list[0].kwargs.get("trace_id"),
        )

    async def test_stream_fetches_history(self, service, mock_repo, mock_registry):
        """测试发送消息前获取历史上下文"""
        conv_id = uuid.uuid4()
        mock_repo.add_message = AsyncMock()
        mock_repo.get_messages = AsyncMock(return_value=[])

        async def empty_stream(*a, **kw):
            return
            yield  # make it an async generator

        mock_client = MagicMock()
        mock_client.chat_completion_stream = empty_stream
        mock_registry.get_client.return_value = mock_client

        _ = [
            c
            async for c in service.send_message_stream_only(
                conv_id, "Q2024010100001", "hello", assistant_type="openclaw"
            )
        ]

        mock_repo.get_messages.assert_called_once_with(conv_id)


# ---------- Jaccard 相似度测试 ----------


class TestJaccardSimilarity:
    """Jaccard 相似度算法测试"""

    def test_jaccard_identical_strings(self):
        """测试完全相同的字符串"""
        result = jaccard_similarity("虚拟机 磁盘 异常", "虚拟机 磁盘 异常")
        assert result == 1.0

    def test_jaccard_similar_strings(self):
        """测试相似字符串 - 验收标准用例"""
        # "虚拟机 磁盘 异常" vs "虚拟机 磁盘 IO 异常"
        # 交集：{虚拟机，磁盘，异常} = 3
        # 并集：{虚拟机，磁盘，异常，IO} = 4
        # Jaccard = 3/4 = 0.75 >= 0.6
        result = jaccard_similarity("虚拟机 磁盘 异常", "虚拟机 磁盘 IO 异常")
        assert result >= 0.6

    def test_jaccard_different_strings(self):
        """测试完全不同的字符串"""
        result = jaccard_similarity("你好世界", "再见地球")
        assert result == 0.0

    def test_jaccard_empty_string(self):
        """测试空字符串"""
        assert jaccard_similarity("", "test") == 0.0
        assert jaccard_similarity("test", "") == 0.0
        assert jaccard_similarity("", "") == 0.0

    def test_jaccard_case_insensitive(self):
        """测试大小写不敏感"""
        result = jaccard_similarity("Hello World", "hello world")
        assert result == 1.0

    def test_jaccard_partial_overlap(self):
        """测试部分重叠"""
        # "CPU 内存 磁盘" vs "CPU 内存 网络"
        # 交集：{CPU, 内存} = 2
        # 并集：{CPU, 内存，磁盘，网络} = 4
        # Jaccard = 2/4 = 0.5
        result = jaccard_similarity("CPU 内存 磁盘", "CPU 内存 网络")
        assert result == 0.5


# ---------- 重复提问检测测试 ----------


@pytest.mark.asyncio
class TestRepeatQuestionDetection:
    """重复提问检测测试"""

    async def test_check_repeat_question_detects_duplicate(self, service, mock_repo):
        """测试检测到重复提问"""
        from app.models.message import Message

        conv_id = uuid.uuid4()
        case_id = "Q2024010100001"

        # Mock 历史消息（相似内容）
        historical_msg = MagicMock(spec=Message)
        historical_msg.content = "虚拟机 磁盘 异常"
        historical_msg.message_id = uuid.uuid4()

        mock_repo.get_recent_user_messages = AsyncMock(return_value=[historical_msg])
        mock_repo.increment_repeat_question_count = AsyncMock()

        await service._check_repeat_question(
            conversation_id=conv_id,
            case_id=case_id,
            content="虚拟机 磁盘 IO 异常",  # 与历史消息相似度 >= 0.6
            current_message_id=uuid.uuid4(),
        )

        mock_repo.increment_repeat_question_count.assert_called_once_with(conv_id)

    async def test_check_repeat_question_no_duplicate(self, service, mock_repo):
        """测试非重复提问"""
        from app.models.message import Message

        conv_id = uuid.uuid4()
        case_id = "Q2024010100001"

        # Mock 历史消息（完全不同内容）
        historical_msg = MagicMock(spec=Message)
        historical_msg.content = "你好世界"
        historical_msg.message_id = uuid.uuid4()

        mock_repo.get_recent_user_messages = AsyncMock(return_value=[historical_msg])
        mock_repo.increment_repeat_question_count = AsyncMock()

        await service._check_repeat_question(
            conversation_id=conv_id,
            case_id=case_id,
            content="再见地球",  # 与历史消息相似度 = 0.0
            current_message_id=uuid.uuid4(),
        )

        mock_repo.increment_repeat_question_count.assert_not_called()

    async def test_check_repeat_question_no_history(self, service, mock_repo):
        """测试无历史消息"""
        conv_id = uuid.uuid4()
        case_id = "Q2024010100001"

        mock_repo.get_recent_user_messages = AsyncMock(return_value=[])
        mock_repo.increment_repeat_question_count = AsyncMock()

        await service._check_repeat_question(
            conversation_id=conv_id,
            case_id=case_id,
            content="第一条消息",
            current_message_id=uuid.uuid4(),
        )

        mock_repo.increment_repeat_question_count.assert_not_called()

    async def test_check_repeat_question_error_handling(self, service, mock_repo):
        """测试错误处理"""
        conv_id = uuid.uuid4()
        case_id = "Q2024010100001"

        mock_repo.get_recent_user_messages = AsyncMock(side_effect=Exception("Database error"))
        mock_repo.increment_repeat_question_count = AsyncMock()

        # 不应抛出异常
        await service._check_repeat_question(
            conversation_id=conv_id,
            case_id=case_id,
            content="测试消息",
            current_message_id=uuid.uuid4(),
        )

        mock_repo.increment_repeat_question_count.assert_not_called()
