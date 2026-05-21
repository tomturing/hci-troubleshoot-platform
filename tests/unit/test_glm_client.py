"""
GLMClient 单元测试

覆盖：
  - _safe_parse_json 对尾随逗号/缺少引号的处理
  - chat() 方法（mock OpenAI client）
  - 429 限流重试逻辑
  - tool_calls 列表的正确解析
"""

import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from openai import APIConnectionError, RateLimitError

# conversation-service 路径常量，供 fixture 内部使用
# 注意：不在模块级做 sys.modules 清理——pytest 会先收集所有模块再跑测试，
# 模块级清理会在 test_case_service 的测试执行前破坏 app 命名空间
_CONV_SVC = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "backend", "conversation-service")
)

# agent-service 路径（PR #309 新增，glm_client 已迁移至此）
_AGENT_SVC = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "backend", "agent-service")
)


def _use_app() -> None:
    """确保 sys.modules 中的 app 指向正确的服务（在 fixture 内调用）"""
    # 优先尝试 conversation-service，若无 glm_client 则使用 agent-service
    for svc_path in [_CONV_SVC, _AGENT_SVC]:
        app_path = os.path.join(svc_path, "app")
        if os.path.exists(os.path.join(app_path, "core", "glm_client.py")):
            _expect = os.path.normpath(app_path)
            _actual = (
                os.path.normpath(getattr(sys.modules.get("app"), "__path__", [""])[0])
                if "app" in sys.modules
                else ""
            )
            if _expect != _actual:
                for _k in list(sys.modules):
                    if _k == "app" or _k.startswith("app."):
                        del sys.modules[_k]
                if svc_path in sys.path:
                    sys.path.remove(svc_path)
                sys.path.insert(0, svc_path)
            return


# ─── 工具函数 _safe_parse_json 测试 ──────────────────────────────────────────

@pytest.fixture
def glm_client():
    """创建 GLMClient 实例（mock OpenAI client，不发网络请求）"""
    # 在测试执行阶段切换 app 指向，优先使用 conversation-service 或 agent-service
    _use_app()
    from app.core.glm_client import GLMClient
    with patch("app.core.glm_client.AsyncOpenAI"):
        client = GLMClient(
            base_url="http://localhost:11434",
            api_key="test-key",
            model="glm-5",
        )
    return client


class TestSafeParseJson:
    """_safe_parse_json 的健壮性测试"""

    def test_valid_json_parses_correctly(self, glm_client):
        result = glm_client._safe_parse_json('{"key": "value"}', "call_001")
        assert result == {"key": "value"}

    def test_trailing_comma_in_object_is_fixed(self, glm_client):
        result = glm_client._safe_parse_json('{"key": "value",}', "call_002")
        assert result == {"key": "value"}

    def test_trailing_comma_in_array_is_fixed(self, glm_client):
        result = glm_client._safe_parse_json('{"items": [1, 2, 3,]}', "call_003")
        assert result == {"items": [1, 2, 3]}

    def test_empty_string_returns_empty_dict(self, glm_client):
        result = glm_client._safe_parse_json("", "call_004")
        assert result == {}

    def test_unparseable_json_falls_back_to_raw(self, glm_client):
        """无法解析的 JSON 应降级为 {"_raw": ...}，不抛异常"""
        raw = "这不是JSON {{{完全错误"
        result = glm_client._safe_parse_json(raw, "call_005")
        assert "_raw" in result
        assert result["_raw"] == raw

    def test_nested_json_parses_correctly(self, glm_client):
        raw = '{"cluster_id": "c-001", "limit": 10}'
        result = glm_client._safe_parse_json(raw, "call_006")
        assert result == {"cluster_id": "c-001", "limit": 10}


class TestGLMClientChat:
    """chat() 方法测试"""

    @pytest.mark.asyncio
    async def test_non_stream_chat_returns_llm_response(self, glm_client):
        """mock OpenAI response，验证 chat() 正确解析"""
        from app.core.glm_client import LLMResponse

        mock_choice = MagicMock()
        mock_choice.message.content = "这是诊断结果"
        mock_choice.message.tool_calls = None
        mock_choice.finish_reason = "stop"

        mock_usage = MagicMock()
        mock_usage.model_dump.return_value = {"prompt_tokens": 100, "completion_tokens": 50}

        mock_resp = MagicMock()
        mock_resp.choices = [mock_choice]
        mock_resp.usage = mock_usage

        glm_client.client.chat.completions.create = AsyncMock(return_value=mock_resp)

        result = await glm_client.chat(messages=[{"role": "user", "content": "有什么告警？"}])

        assert isinstance(result, LLMResponse)
        assert result.content == "这是诊断结果"
        assert result.finish_reason == "stop"
        assert result.tool_calls == []

    @pytest.mark.asyncio
    async def test_tool_calls_are_parsed_correctly(self, glm_client):
        """验证 tool_calls 列表的正确解析"""
        from app.core.glm_client import ToolCall

        mock_tc = MagicMock()
        mock_tc.id = "call_abc123"
        mock_tc.function.name = "get_active_alerts"
        mock_tc.function.arguments = '{"limit": 10}'

        mock_choice = MagicMock()
        mock_choice.message.content = None
        mock_choice.message.tool_calls = [mock_tc]
        mock_choice.finish_reason = "tool_calls"

        mock_usage = MagicMock()
        mock_usage.model_dump.return_value = {}

        mock_resp = MagicMock()
        mock_resp.choices = [mock_choice]
        mock_resp.usage = mock_usage

        glm_client.client.chat.completions.create = AsyncMock(return_value=mock_resp)

        result = await glm_client.chat(
            messages=[{"role": "user", "content": "查告警"}],
            tools=[{"type": "function", "function": {"name": "get_active_alerts"}}],
        )

        assert len(result.tool_calls) == 1
        tc = result.tool_calls[0]
        assert isinstance(tc, ToolCall)
        assert tc.id == "call_abc123"
        assert tc.name == "get_active_alerts"
        assert tc.args == {"limit": 10}

    @pytest.mark.asyncio
    async def test_rate_limit_retries_three_times(self, glm_client):
        """429 RateLimitError 时应重试最多 MAX_RETRIES 次，sleep 调用次数正确"""
        rate_limit_error = RateLimitError(
            message="rate limit",
            response=MagicMock(status_code=429, headers={}),
            body=None,
        )
        glm_client.client.chat.completions.create = AsyncMock(side_effect=rate_limit_error)

        with patch("app.core.glm_client.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            with pytest.raises(RateLimitError):
                await glm_client.chat(messages=[{"role": "user", "content": "test"}])

            # MAX_RETRIES=3，前两次失败后应 sleep，第三次直接抛异常
            assert mock_sleep.call_count == 2
            # 延迟应为指数退避：1.0s, 2.0s
            assert mock_sleep.call_args_list[0][0][0] == 1.0
            assert mock_sleep.call_args_list[1][0][0] == 2.0

    @pytest.mark.asyncio
    async def test_connection_error_raises_immediately(self, glm_client):
        """APIConnectionError 不重试，直接抛异常"""
        glm_client.client.chat.completions.create = AsyncMock(
            side_effect=APIConnectionError(request=MagicMock())
        )

        with pytest.raises(APIConnectionError):
            await glm_client.chat(messages=[{"role": "user", "content": "test"}])
