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
from app.services.conversation_service import ConversationService


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
        mock_repo.create_conversation = AsyncMock(
            return_value=MagicMock(spec=Conversation)
        )
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
            if False:
                yield ""

        mock_client = MagicMock()
        mock_client.chat_completion_stream = empty_stream
        mock_registry.get_client.return_value = mock_client

        chunks = [c async for c in service.send_message_stream_only(
            conv_id, "Q2024010100001", "hello", assistant_type="openclaw"
        )]

        mock_repo.get_messages.assert_called_once_with(conv_id)


# ---------- _format_interactive_request_content ----------

class TestFormatInteractiveRequestContent:
    """_format_interactive_request_content 静态方法测试"""

    def _make_event(self, kind="info_request", prompt="", options=None, metadata=None):
        from app.core.brain_port import BrainInteractiveRequest
        return BrainInteractiveRequest(
            request_id="req-1",
            acp_session_id="sess-1",
            kind=kind,
            title="",
            prompt=prompt,
            options=options or [],
            custom_input=True,
            metadata=metadata or {},
        )

    def test_info_request_with_prompt(self):
        """info_request 类型：使用 prompt 作为问题"""
        event = self._make_event(kind="info_request", prompt="当前节点的内存使用率是多少？")
        result = ConversationService._format_interactive_request_content(event)
        assert "❓ 信息确认" in result
        assert "当前节点的内存使用率是多少？" in result

    def test_info_request_with_meta_question(self):
        """info_request 类型：metadata.question 优先于 prompt"""
        event = self._make_event(
            kind="info_request",
            prompt="fallback",
            metadata={"question": "CPU 是否超过阈值？", "context": "节点处于高负载状态"},
        )
        result = ConversationService._format_interactive_request_content(event)
        assert "CPU 是否超过阈值？" in result
        assert "背景说明" in result
        assert "节点处于高负载状态" in result
        assert "fallback" not in result

    def test_sop_step_with_full_meta(self):
        """sop_step 类型：包含路径/目标/预期/指引/反馈"""
        event = self._make_event(
            kind="sop_step",
            prompt="请确认执行结果",
            metadata={
                "route": "云计算 > 虚拟机",
                "operationGoal": "释放资源",
                "expectedResult": "虚拟机成功启动",
                "executionGuidance": "点击开机按钮",
                "feedbackRequest": "虚拟机是否已启动？",
            },
        )
        result = ConversationService._format_interactive_request_content(event)
        assert "📋 SOP 操作步骤确认" in result
        assert "云计算 > 虚拟机" in result
        assert "释放资源" in result
        assert "虚拟机成功启动" in result
        assert "点击开机按钮" in result
        assert "虚拟机是否已启动？" in result

    def test_options_rendered(self):
        """选项列表正常渲染"""
        event = self._make_event(
            kind="info_request",
            prompt="请选择",
            options=[
                {"optionId": "1", "name": "虚拟机成功启动"},
                {"optionId": "2", "name": "启动失败"},
            ],
        )
        result = ConversationService._format_interactive_request_content(event)
        assert "1. 虚拟机成功启动" in result
        assert "2. 启动失败" in result


# ---------- _format_interactive_response_content ----------

class TestFormatInteractiveResponseContent:
    """_format_interactive_response_content 静态方法测试"""

    def test_selected_with_label(self):
        """选项选择：优先用 optionLabel"""
        result = ConversationService._format_interactive_response_content(
            {"outcome": "selected", "optionId": "1", "optionLabel": "虚拟机成功启动，状态变为运行中"}
        )
        assert result == "[操作选择] 虚拟机成功启动，状态变为运行中"

    def test_selected_fallback_to_id(self):
        """选项选择：无 optionLabel 时降级到 optionId"""
        result = ConversationService._format_interactive_response_content(
            {"outcome": "selected", "optionId": "2"}
        )
        assert result == "[操作选择] 2"

    def test_free_text(self):
        """自由文本输入"""
        result = ConversationService._format_interactive_response_content(
            {"outcome": "free_text", "text": "内存已释放，节点恢复正常"}
        )
        assert result == "[补充输入] 内存已释放，节点恢复正常"

    def test_unknown_outcome(self):
        """未知 outcome 类型：回退到 JSON 序列化"""
        result = ConversationService._format_interactive_response_content(
            {"outcome": "custom", "data": "x"}
        )
        assert "[交互响应]" in result


# ---------- submit_interactive_response 落库 ----------

@pytest.mark.asyncio
class TestSubmitInteractiveResponsePersist:
    """submit_interactive_response 成功后落库到 message 表"""

    async def test_user_message_saved_on_success(self, service, mock_repo):
        """ACP 回传成功后，用户响应以 user 角色写入 message 表"""
        from unittest.mock import AsyncMock, MagicMock

        from app.models.conversation import Conversation
        from app.models.message import MessageRole

        conv_id = uuid.uuid4()
        case_id = "Q2024010100001"

        # Mock BrainRouter + OpsAgentBrainAdapter
        mock_adapter = AsyncMock()
        mock_adapter.submit_acp_response = AsyncMock(return_value=True)
        mock_router = MagicMock()
        mock_router.get_ops_agent_adapter.return_value = mock_adapter

        # Mock conversation 查询
        mock_conv = MagicMock(spec=Conversation)
        mock_conv.case_id = case_id
        mock_repo.get_conversation = AsyncMock(return_value=mock_conv)
        mock_repo.add_message = AsyncMock()

        service._brain_router = mock_router

        outcome = {"outcome": "selected", "optionId": "1", "optionLabel": "虚拟机成功启动"}
        result = await service.submit_interactive_response(
            conversation_id=conv_id,
            request_id="req-abc",
            acp_session_id="sess-abc",
            outcome=outcome,
        )

        assert result is True

        # 验证 add_message 被调用，且 role=user，content 包含选项文本
        mock_repo.add_message.assert_called_once()
        call_kwargs = mock_repo.add_message.call_args.kwargs
        assert call_kwargs["role"] == MessageRole.user
        assert "虚拟机成功启动" in call_kwargs["content"]
        assert call_kwargs["conversation_id"] == conv_id
        assert call_kwargs["case_id"] == case_id
        assert call_kwargs["metadata"]["kind"] == "interactive_response"

    async def test_no_message_on_adapter_unavailable(self, service, mock_repo):
        """ops-agent 适配器不可用时：返回 False，不落库"""
        mock_router = MagicMock()
        mock_router.get_ops_agent_adapter.return_value = None
        service._brain_router = mock_router
        mock_repo.add_message = AsyncMock()

        result = await service.submit_interactive_response(
            conversation_id=uuid.uuid4(),
            request_id="req-x",
            acp_session_id="sess-x",
            outcome={"outcome": "selected", "optionId": "1"},
        )

        assert result is False
        mock_repo.add_message.assert_not_called()


# ---------- BrainRouter 路径落库 metadata 结构验证 ----------

@pytest.mark.asyncio
class TestBrainRouterInteractiveRequestMetadata:
    """验证 BrainRouter 路径下 BrainInteractiveRequest 落库时 metadata 含完整 event 嵌套结构"""

    async def test_save_message_bg_contains_event_nested_structure(self, service, mock_repo):
        """BrainRouter yield BrainInteractiveRequest 时，_save_message_bg 传入的
        metadata 必须为 { kind, event: { requestId, acpSessionId, kind, title,
        prompt, options, customInput, metadata } } 嵌套格式，
        而非旧的扁平结构（interactiveKind/requestId 等顶层字段），
        否则前端历史加载时 metadata.event 为 undefined，气泡渲染为空。
        """
        import asyncio
        from unittest.mock import AsyncMock, MagicMock

        from app.core.brain_port import BrainInteractiveRequest

        conv_id = uuid.uuid4()
        case_id = "Q2024010100001"

        # 构造 BrainInteractiveRequest 事件
        ir_event = BrainInteractiveRequest(
            request_id="req-brain-001",
            acp_session_id="sess-brain-001",
            kind="info_request",
            title="信息确认",
            prompt="虚拟机具体出现了什么异常现象？",
            options=[
                {"optionId": "1", "name": "无法启动"},
                {"optionId": "2", "name": "网络异常"},
            ],
            custom_input=True,
            metadata={"question": "虚拟机具体出现了什么异常现象？", "context": "排障初始阶段"},
        )

        # Mock BrainRouter.process yield BrainInteractiveRequest
        async def fake_brain_process(*args, **kwargs):
            yield ir_event

        mock_router = MagicMock()
        mock_router.process = fake_brain_process
        service._brain_router = mock_router

        # Mock repo 基础依赖
        mock_repo.add_message = AsyncMock()
        mock_repo.get_messages = AsyncMock(return_value=[])
        mock_repo.get_conversation = AsyncMock()

        # session_factory 置 None 使 _save_message_bg 走 self.repository 路径
        service.session_factory = None

        # 收集流输出
        chunks = []
        async for chunk in service.send_message_stream_only(
            conversation_id=conv_id,
            case_id=case_id,
            content="虚拟机有异常",
            assistant_type="ops-agent",
        ):
            chunks.append(chunk)

        # 等待后台 asyncio.create_task 完成
        await asyncio.sleep(0)

        # 找到 kind=interactive_request 的落库调用
        ir_calls = [
            call for call in mock_repo.add_message.call_args_list
            if call.kwargs.get("metadata", {}).get("kind") == "interactive_request"
        ]
        assert len(ir_calls) >= 1, "_save_message_bg 应当以 kind=interactive_request 落库一次"

        saved_metadata = ir_calls[0].kwargs["metadata"]

        # 核心断言：必须有 event 嵌套，不能是旧的扁平结构
        assert "event" in saved_metadata, (
            "落库的 metadata 缺少 event 字段，前端历史加载时 metadata.event 将为 undefined"
        )
        assert "interactiveKind" not in saved_metadata, (
            "落库的 metadata 不应包含旧版 interactiveKind 字段（扁平结构已废弃）"
        )

        ev = saved_metadata["event"]
        assert ev["requestId"] == "req-brain-001"
        assert ev["acpSessionId"] == "sess-brain-001"
        assert ev["kind"] == "info_request"
        assert ev["title"] == "信息确认"
        assert ev["prompt"] == "虚拟机具体出现了什么异常现象？"
        assert len(ev["options"]) == 2
        assert ev["customInput"] is True
        assert ev["metadata"]["question"] == "虚拟机具体出现了什么异常现象？"

