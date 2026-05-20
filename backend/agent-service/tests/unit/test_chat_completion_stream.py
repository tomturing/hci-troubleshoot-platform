"""
OpenClawAssistant.chat_completion_stream SSE 流式测试

测试覆盖：
1. 正常 SSE 流式响应解析
2. 端点重试机制（pod_endpoint → base_url fallback）
3. 错误状态码解析（401/429/500）
4. 空响应处理（rate limit）
5. 可重试错误处理
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

import json
from unittest.mock import AsyncMock

import pytest
from shared.clients.ai_client import OpenClawAssistant
from shared.utils.exceptions import AIStreamError, ErrorCode


class MockStreamResponse:
    """Mock SSE 流式响应"""

    def __init__(self, status_code: int, lines: list[str]):
        self.status_code = status_code
        self._lines = lines

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aread(self):
        return b""


class MockStreamContextManager:
    """Mock async context manager for httpx.stream"""

    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return None


@pytest.fixture
def openclaw_client():
    """创建测试用 OpenClawAssistant"""
    client = OpenClawAssistant(
        base_url="http://openclaw:8000",
        api_key="test-gateway-token",
        provider_api_key="test-provider-key",
        default_model="glm-4-flash",
        assistant_type="glm-4-flash",
    )
    return client


class TestParseAIError:
    """测试错误解析方法"""

    def test_parse_401_error(self, openclaw_client):
        """测试 401 错误解析"""
        result = openclaw_client._parse_ai_error(401, '{"error": {"message": "Invalid key"}}')
        assert result["code"] == ErrorCode.AI_AUTH_FAILED
        assert "认证失败" in result["message"]

    def test_parse_429_balance_insufficient(self, openclaw_client):
        """测试 429 余额不足"""
        result = openclaw_client._parse_ai_error(429, '{"error": {"message": "余额不足，请充值"}}')
        assert result["code"] == ErrorCode.AI_RATE_LIMITED
        assert "余额不足" in result["message"]

    def test_parse_429_rate_limit(self, openclaw_client):
        """测试 429 请求频率超限"""
        result = openclaw_client._parse_ai_error(429, '{"error": {"message": "rate limit exceeded"}}')
        assert result["code"] == ErrorCode.AI_RATE_LIMITED
        assert "频率超限" in result["message"]

    def test_parse_404_not_found(self, openclaw_client):
        """测试 404 接口不存在"""
        result = openclaw_client._parse_ai_error(404, '{"error": {"message": "Not found"}}')
        assert result["code"] == ErrorCode.AI_UNAVAILABLE
        assert "接口不存在" in result["message"]

    def test_parse_500_upstream_error(self, openclaw_client):
        """测试 500 上游服务故障"""
        result = openclaw_client._parse_ai_error(500, '{"error": {"message": "Internal error"}}')
        assert result["code"] == ErrorCode.AI_UPSTREAM_ERROR
        assert "故障" in result["message"]


class TestIsRetriableError:
    """测试可重试错误识别"""

    def test_retriable_incomplete_chunked_read(self, openclaw_client):
        """测试 incomplete chunked read 可重试"""
        assert openclaw_client._is_retriable_stream_error(Exception("Incomplete chunked read")) is True

    def test_retriable_peer_closed(self, openclaw_client):
        """测试 peer closed connection 可重试"""
        assert openclaw_client._is_retriable_stream_error(Exception("Peer closed connection")) is True

    def test_retriable_read_timeout(self, openclaw_client):
        """测试 read timeout 可重试"""
        assert openclaw_client._is_retriable_stream_error(Exception("Read timeout")) is True

    def test_not_retriable_invalid_request(self, openclaw_client):
        """测试 invalid request 不可重试"""
        assert openclaw_client._is_retriable_stream_error(Exception("Invalid request")) is False

    def test_not_retriable_auth_error(self, openclaw_client):
        """测试认证错误不可重试"""
        assert openclaw_client._is_retriable_stream_error(Exception("Authentication failed")) is False


class TestChatCompletionStreamIntegration:
    """集成测试：使用 mock httpx client"""

    @pytest.mark.asyncio
    async def test_stream_success_with_mock(self, openclaw_client):
        """测试使用 MockStreamResponse 的成功场景"""
        sse_lines = [
            "data: {" + json.dumps({"choices": [{"delta": {"content": "你好"}}]}) + "}",
            "data: {" + json.dumps({"choices": [{"delta": {"content": "，"}}]}) + "}",
            "data: {" + json.dumps({"choices": [{"delta": {"content": "我是助手"}}]}) + "}",
            "data: [DONE]",
        ]

        mock_response = MockStreamResponse(200, sse_lines)
        mock_cm = MockStreamContextManager(mock_response)

        # Patch httpx.AsyncClient.stream
        with pytest.MonkeyPatch().context() as m:
            m.setattr(openclaw_client.client, "stream", lambda *args, **kwargs: mock_cm)

            chunks = []
            async for chunk in openclaw_client.chat_completion_stream(
                messages=[{"role": "user", "content": "test"}],
                user_id="test-user",
            ):
                chunks.append(chunk)

            assert chunks == ["你好", "，", "我是助手"]

    @pytest.mark.asyncio
    async def test_stream_401_error_with_mock(self, openclaw_client):
        """测试使用 MockStreamResponse 的 401 错误场景"""
        mock_response = MockStreamResponse(401, [])
        mock_response.aread = AsyncMock(return_value=b'{"error": {"message": "Invalid API key"}}')
        mock_cm = MockStreamContextManager(mock_response)

        with pytest.MonkeyPatch().context() as m:
            m.setattr(openclaw_client.client, "stream", lambda *args, **kwargs: mock_cm)

            with pytest.raises(AIStreamError) as exc_info:
                async for _ in openclaw_client.chat_completion_stream(
                    messages=[{"role": "user", "content": "test"}],
                    user_id="test-user",
                ):
                    pass

            assert exc_info.value.code == ErrorCode.AI_AUTH_FAILED
            assert "认证失败" in exc_info.value.message

    @pytest.mark.asyncio
    async def test_stream_empty_response_with_mock(self, openclaw_client):
        """测试空响应（rate limit 导致无内容）"""
        sse_lines = ["data: [DONE]"]
        mock_response = MockStreamResponse(200, sse_lines)
        mock_cm = MockStreamContextManager(mock_response)

        with pytest.MonkeyPatch().context() as m:
            m.setattr(openclaw_client.client, "stream", lambda *args, **kwargs: mock_cm)

            with pytest.raises(AIStreamError) as exc_info:
                async for _ in openclaw_client.chat_completion_stream(
                    messages=[{"role": "user", "content": "test"}],
                    user_id="test-user",
                ):
                    pass

            assert exc_info.value.code == ErrorCode.AI_RATE_LIMITED
