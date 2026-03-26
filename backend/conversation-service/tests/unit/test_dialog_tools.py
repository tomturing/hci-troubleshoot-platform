"""
DialogTools 单元测试

测试 ask_user 对话工具
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


class MockRedis:
    """模拟 Redis 客户端"""

    def __init__(self):
        self._data = {}
        self._brpop_queue = []

    async def set(self, key: str, value: str, ex: int = None):
        self._data[key] = value

    async def get(self, key: str):
        return self._data.get(key)

    async def delete(self, key: str):
        if key in self._data:
            del self._data[key]

    async def lpush(self, key: str, value: str):
        if key not in self._data:
            self._data[key] = []
        if isinstance(self._data[key], str):
            self._data[key] = [self._data[key]]
        self._data[key].insert(0, value)

    async def expire(self, key: str, seconds: int):
        pass

    # 模拟 BRPOP 行为
    def set_brpop_result(self, key: str, value: str):
        self._brpop_queue.append((key, value))

    async def brpop(self, key: str, timeout: int = 0):
        # 简化的模拟：直接返回结果
        if self._brpop_queue:
            return self._brpop_queue.pop(0)
        return None


class MockConfirmService:
    """模拟确认服务"""

    def __init__(self, confirm_result: bool = True):
        self._confirm_result = confirm_result

    async def request_confirm(
        self,
        session_id: str,
        tool_name: str,
        tool_args: dict,
        risk_level: int,
    ) -> bool:
        return self._confirm_result


class MockSSEEmitter:
    """模拟 SSE 发射器"""

    def __init__(self):
        self.emitted_events = []

    async def emit(self, event_type: str, data: dict):
        self.emitted_events.append({"type": event_type, "data": data})


class TestDialogTools:
    """DialogTools 测试用例"""

    @pytest.mark.asyncio
    async def test_ask_user_without_redis(self):
        """测试 Redis 未配置时返回 None"""
        from app.adapters.dialog_tools import DialogTools

        tools = DialogTools(redis=None)

        result = await tools.ask_user(
            session_id="session-001",
            question="请确认您的 VM 名称",
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_ask_user_without_confirm_service(self):
        """测试 confirm_service 未配置时返回 None"""
        from app.adapters.dialog_tools import DialogTools

        redis = MockRedis()
        tools = DialogTools(redis=redis, confirm_service=None)

        result = await tools.ask_user(
            session_id="session-001",
            question="请确认您的 VM 名称",
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_ask_user_sends_event(self):
        """测试 ask_user 发送 SSE 事件"""
        from app.adapters.dialog_tools import DialogTools

        redis = MockRedis()
        confirm_service = MockConfirmService(confirm_result=False)
        sse_emitter = MockSSEEmitter()

        tools = DialogTools(
            redis=redis,
            confirm_service=confirm_service,
            sse_emitter=sse_emitter,
        )

        await tools.ask_user(
            session_id="session-001",
            question="请确认您的 VM 名称",
        )

        # 验证 SSE 事件发送
        assert len(sse_emitter.emitted_events) == 1
        event = sse_emitter.emitted_events[0]
        assert event["type"] == "user_question"
        assert event["data"]["question"] == "请确认您的 VM 名称"

    @pytest.mark.asyncio
    async def test_ask_user_writes_to_redis(self):
        """测试 ask_user 写入 Redis"""
        from app.adapters.dialog_tools import DialogTools

        redis = MockRedis()
        confirm_service = MockConfirmService(confirm_result=False)

        tools = DialogTools(
            redis=redis,
            confirm_service=confirm_service,
            sse_emitter=None,  # 不测试 SSE
        )

        await tools.ask_user(
            session_id="session-001",
            question="请确认您的 VM 名称",
            options=["VM1", "VM2", "VM3"],
        )

        # 验证 Redis 写入
        key = "ask_user:session-001"
        assert key in redis._data
        data = json.loads(redis._data[key])
        assert data["question"] == "请确认您的 VM 名称"
        assert data["options"] == ["VM1", "VM2", "VM3"]

    @pytest.mark.asyncio
    async def test_ask_user_user_confirms(self):
        """测试用户确认后返回确认"""
        from app.adapters.dialog_tools import DialogTools

        redis = MockRedis()
        confirm_service = MockConfirmService(confirm_result=True)

        # 设置回复
        reply_key = "ask_user_reply:session-001"
        redis._data[reply_key] = json.dumps({"reply": "VM1"})

        tools = DialogTools(
            redis=redis,
            confirm_service=confirm_service,
            sse_emitter=None,
        )

        result = await tools.ask_user(
            session_id="session-001",
            question="请选择 VM",
        )

        assert result == "VM1"

    @pytest.mark.asyncio
    async def test_ask_user_user_cancels(self):
        """测试用户取消时返回 None"""
        from app.adapters.dialog_tools import DialogTools

        redis = MockRedis()
        confirm_service = MockConfirmService(confirm_result=False)

        tools = DialogTools(
            redis=redis,
            confirm_service=confirm_service,
            sse_emitter=None,
        )

        result = await tools.ask_user(
            session_id="session-001",
            question="请选择 VM",
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_ask_user_no_reply_confirms(self):
        """测试无专门回复时返回 confirmed"""
        from app.adapters.dialog_tools import DialogTools

        redis = MockRedis()
        confirm_service = MockConfirmService(confirm_result=True)

        tools = DialogTools(
            redis=redis,
            confirm_service=confirm_service,
            sse_emitter=None,
        )

        # 没有设置 reply key
        result = await tools.ask_user(
            session_id="session-001",
            question="请确认",
        )

        # 应返回 "confirmed"（表示用户确认但无具体回复）
        assert result == "confirmed"

    @pytest.mark.asyncio
    async def test_ask_user_with_options(self):
        """测试带选项的提问"""
        from app.adapters.dialog_tools import DialogTools

        redis = MockRedis()
        confirm_service = MockConfirmService(confirm_result=True)
        sse_emitter = MockSSEEmitter()

        options = ["选项 A", "选项 B", "选项 C"]

        tools = DialogTools(
            redis=redis,
            confirm_service=confirm_service,
            sse_emitter=sse_emitter,
        )

        await tools.ask_user(
            session_id="session-001",
            question="请选择一个选项",
            options=options,
        )

        # 验证 SSE 事件包含选项
        event = sse_emitter.emitted_events[0]
        assert event["data"]["options"] == options


class TestAskUserToolDefinition:
    """ask_user 工具定义测试"""

    def test_tool_definition_structure(self):
        """测试工具定义结构"""
        from app.adapters.dialog_tools import ASK_USER_TOOL_DEFINITION

        assert ASK_USER_TOOL_DEFINITION["name"] == "ask_user"
        assert "description" in ASK_USER_TOOL_DEFINITION
        assert "parameters" in ASK_USER_TOOL_DEFINITION
        assert ASK_USER_TOOL_DEFINITION["risk_level"] == 1
        assert ASK_USER_TOOL_DEFINITION["policy"] == "auto"
        assert ASK_USER_TOOL_DEFINITION["category"] == "dialog"

    def test_tool_parameters(self):
        """测试工具参数定义"""
        from app.adapters.dialog_tools import ASK_USER_TOOL_DEFINITION

        params = ASK_USER_TOOL_DEFINITION["parameters"]
        assert "type" in params
        assert params["type"] == "object"
        assert "properties" in params
        assert "question" in params["properties"]
        assert "options" in params["properties"]
        assert "required" in params
        assert "question" in params["required"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
