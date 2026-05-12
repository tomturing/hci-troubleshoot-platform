"""
OpsAgentBrainAdapter._consume_events 单元测试

覆盖 2026-05 修复：session/done 到达但无文本产出时触发 BrainUnavailableError（fallback）。
"""
import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from app.adapters.ops_agent_brain_adapter import OpsAgentBrainAdapter
from app.core.brain_port import BrainTextChunk, BrainUnavailableError

# ── 构造辅助函数 ────────────────────────────────────────────────────────────


def _sse_line(payload: dict) -> str:
    """构造单条 SSE data 行（不含末尾换行）。"""
    return f"data: {json.dumps(payload, ensure_ascii=False)}"


def _make_agent_message_chunk(text: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {
            "sessionId": "test-session",
            "update": {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": text},
            },
        },
    }


def _make_session_done(stop_reason: str = "end_turn") -> dict:
    return {
        "jsonrpc": "2.0",
        "method": "session/done",
        "params": {"sessionId": "test-session", "stopReason": stop_reason},
    }


def _make_mock_stream_resp(lines: list[str]):
    """构造 httpx stream 响应 mock，aiter_lines 返回给定行列表。"""
    mock_resp = AsyncMock()
    mock_resp.status_code = 200

    async def aiter_lines():
        for line in lines:
            yield line

    mock_resp.aiter_lines = aiter_lines
    return mock_resp


def _make_adapter() -> OpsAgentBrainAdapter:
    adapter = OpsAgentBrainAdapter(base_url="http://ops-agent:8080")
    return adapter


# ── 测试套件 ──────────────────────────────────────────────────────────────


class TestConsumeEvents:
    """_consume_events 的行为测试。"""

    @pytest.mark.asyncio
    async def test_text_chunk_is_yielded_on_agent_message_chunk(self):
        """正常文本事件应被翻译为 BrainTextChunk。"""
        adapter = _make_adapter()
        lines = [
            _sse_line(_make_agent_message_chunk("你好，正在分析问题...")),
            _sse_line(_make_agent_message_chunk("分析完成，建议检查网络配置。")),
            _sse_line(_make_session_done("end_turn")),
        ]
        mock_resp = _make_mock_stream_resp(lines)

        events = []
        with patch.object(adapter._client, "stream") as mock_stream:
            mock_stream.return_value.__aenter__ = AsyncMock(return_value=mock_resp)
            mock_stream.return_value.__aexit__ = AsyncMock(return_value=False)
            async for ev in adapter._consume_events("test-session", {}):
                events.append(ev)

        assert len(events) == 2
        assert all(isinstance(ev, BrainTextChunk) for ev in events)
        assert events[0].content == "你好，正在分析问题..."
        assert events[1].content == "分析完成，建议检查网络配置。"

    @pytest.mark.asyncio
    async def test_session_done_without_content_raises_unavailable(self):
        """
        session/done 到达但未产出任何文本时，应抛出 BrainUnavailableError。

        根因：ops-agent execute_task() 内部异常被 except 吞掉，stop_reason 仍为
        "end_turn"，如不主动抛错则 HTP fallback 永远不触发，导致空白气泡。
        """
        adapter = _make_adapter()
        # 只有 session/done，没有任何 agent_message_chunk
        lines = [
            _sse_line(_make_session_done("end_turn")),
        ]
        mock_resp = _make_mock_stream_resp(lines)

        with patch.object(adapter._client, "stream") as mock_stream:
            mock_stream.return_value.__aenter__ = AsyncMock(return_value=mock_resp)
            mock_stream.return_value.__aexit__ = AsyncMock(return_value=False)
            with pytest.raises(BrainUnavailableError) as exc_info:
                async for _ in adapter._consume_events("test-session", {}):
                    pass

        assert "session/done without content" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_session_done_without_content_refusal_raises_unavailable(self):
        """stopReason='refusal' 且无文本时同样应抛出 BrainUnavailableError。"""
        adapter = _make_adapter()
        lines = [
            _sse_line(_make_session_done("refusal")),
        ]
        mock_resp = _make_mock_stream_resp(lines)

        with patch.object(adapter._client, "stream") as mock_stream:
            mock_stream.return_value.__aenter__ = AsyncMock(return_value=mock_resp)
            mock_stream.return_value.__aexit__ = AsyncMock(return_value=False)
            with pytest.raises(BrainUnavailableError):
                async for _ in adapter._consume_events("test-session", {}):
                    pass

    @pytest.mark.asyncio
    async def test_session_done_after_content_returns_normally(self):
        """有文本产出后遇到 session/done 应正常 return，不抛出异常。"""
        adapter = _make_adapter()
        lines = [
            _sse_line(_make_agent_message_chunk("有内容")),
            _sse_line(_make_session_done("end_turn")),
        ]
        mock_resp = _make_mock_stream_resp(lines)

        events = []
        with patch.object(adapter._client, "stream") as mock_stream:
            mock_stream.return_value.__aenter__ = AsyncMock(return_value=mock_resp)
            mock_stream.return_value.__aexit__ = AsyncMock(return_value=False)
            async for ev in adapter._consume_events("test-session", {}):
                events.append(ev)

        # 正常完成，不抛异常
        assert len(events) == 1
        assert events[0].content == "有内容"

    @pytest.mark.asyncio
    async def test_empty_text_chunk_is_skipped(self):
        """text 为空字符串的 agent_message_chunk 应被跳过（不视为有效文本产出）。"""
        adapter = _make_adapter()
        lines = [
            # 空文本 chunk（ops-agent 有时会发空 chunk）
            _sse_line(_make_agent_message_chunk("")),
            _sse_line(_make_session_done("end_turn")),
        ]
        mock_resp = _make_mock_stream_resp(lines)

        with patch.object(adapter._client, "stream") as mock_stream:
            mock_stream.return_value.__aenter__ = AsyncMock(return_value=mock_resp)
            mock_stream.return_value.__aexit__ = AsyncMock(return_value=False)
            # 空文本应触发 BrainUnavailableError（不视为有效内容）
            with pytest.raises(BrainUnavailableError):
                async for _ in adapter._consume_events("test-session", {}):
                    pass

    @pytest.mark.asyncio
    async def test_heartbeat_and_empty_lines_are_skipped(self):
        """心跳行和空行应被跳过，不影响 text_emitted 状态。"""
        adapter = _make_adapter()
        lines = [
            ": heartbeat",   # SSE 注释行（心跳）
            "",              # 空行
            _sse_line(_make_agent_message_chunk("实际内容")),
            "",
            _sse_line(_make_session_done("end_turn")),
        ]
        mock_resp = _make_mock_stream_resp(lines)

        events = []
        with patch.object(adapter._client, "stream") as mock_stream:
            mock_stream.return_value.__aenter__ = AsyncMock(return_value=mock_resp)
            mock_stream.return_value.__aexit__ = AsyncMock(return_value=False)
            async for ev in adapter._consume_events("test-session", {}):
                events.append(ev)

        assert len(events) == 1
        assert events[0].content == "实际内容"


class TestSubmitPromptWith409:
    """_submit_prompt 409 Conflict 场景：terminate + retry 逻辑测试。"""

    @pytest.mark.asyncio
    async def test_409_triggers_terminate_then_retry_success(self):
        """
        _submit_prompt 收到 409 时应：
        1. 调用 DELETE /acp/sessions/{id}/prompt（terminate + drain）
        2. 重新 POST /acp/sessions/{id}/prompt
        3. 新 prompt 202 → 正常完成，不触发 BrainUnavailableError

        覆盖场景：页面刷新后 session 有 active_prompt=True（等待 _ops/request_input），
        新消息到来时先终止旧 prompt 再重试，实现会话恢复而非降级。
        """
        adapter = _make_adapter()
        call_sequence = []

        def _mock_request(method, url, **kwargs):
            call_sequence.append((method, url))
            resp = MagicMock(spec=httpx.Response)
            if method == "POST" and "/prompt" in url:
                if len([c for c in call_sequence if c[0] == "POST" and "/prompt" in c[1]]) == 1:
                    # 第一次 POST → 409
                    resp.status_code = 409
                    resp.text = "A prompt is already running"
                else:
                    # 重试 POST → 202
                    resp.status_code = 202
                    resp.text = ""
            elif method == "DELETE" and "/prompt" in url:
                # DELETE（terminate） → 200
                resp.status_code = 200
                resp.json.return_value = {
                    "activePrompt": True,
                    "pendingRequestsCancelled": 1,
                    "terminated": True,
                    "drainedEvents": 2,
                }
                resp.text = ""
            else:
                resp.status_code = 500
                resp.text = "Unexpected"
            return resp

        async def mock_post(url, **kwargs):
            return _mock_request("POST", url, **kwargs)

        async def mock_delete(url, **kwargs):
            return _mock_request("DELETE", url, **kwargs)

        with (
            patch.object(adapter._client, "post", side_effect=mock_post),
            patch.object(adapter._client, "delete", side_effect=mock_delete),
        ):
            # 不应抛出异常
            await adapter._submit_prompt("sess-abc", [{"type": "text", "text": "继续"}], {})

        # 验证调用顺序：POST(409) → DELETE(terminate) → POST(retry 202)
        methods = [(m, "prompt" in u) for m, u in call_sequence]
        assert methods == [("POST", True), ("DELETE", True), ("POST", True)]

    @pytest.mark.asyncio
    async def test_409_retry_also_fails_raises_unavailable(self):
        """
        terminate 后重试仍然失败（非 200/202）时应抛出 BrainUnavailableError，
        不再进一步重试（防止无限循环）。
        """
        adapter = _make_adapter()

        async def mock_post(url, **kwargs):
            resp = MagicMock(spec=httpx.Response)
            resp.status_code = 409
            resp.text = "Still busy"
            return resp

        async def mock_delete(url, **kwargs):
            resp = MagicMock(spec=httpx.Response)
            resp.status_code = 200
            resp.json.return_value = {"activePrompt": True, "terminated": True, "drainedEvents": 0}
            resp.text = ""
            return resp

        with (
            patch.object(adapter._client, "post", side_effect=mock_post),
            patch.object(adapter._client, "delete", side_effect=mock_delete),
        ):
            with pytest.raises(BrainUnavailableError) as exc_info:
                await adapter._submit_prompt("sess-abc", [{"type": "text", "text": "继续"}], {})

        assert "重试后仍失败" in str(exc_info.value)


class TestResumeEventStream:
    """resume_event_stream Bug1 修复验证：activePrompt=false 时不再跳过 outbox 消费。"""

    def _make_state_resp(self, active_prompt: bool, status: int = 200) -> MagicMock:
        """构造 GET /state 响应 mock（同步 MagicMock，与 httpx 实际行为一致）。"""
        mock = MagicMock()
        mock.status_code = status
        mock.json.return_value = {"activePrompt": active_prompt}
        return mock

    @pytest.mark.asyncio
    async def test_resume_skips_session_when_404(self):
        """session 不存在（404）时，resume_event_stream 应立即返回（不消费）。"""
        adapter = _make_adapter()
        state_resp = self._make_state_resp(False, status=404)

        with patch.object(adapter._client, "get", new=AsyncMock(return_value=state_resp)) as mock_get:
            events = []
            async for ev in adapter.resume_event_stream("sess-404"):
                events.append(ev)

        # 只有一次 GET state 调用，没有 GET events 调用
        assert mock_get.call_count == 1
        assert events == []

    @pytest.mark.asyncio
    async def test_resume_consumes_outbox_even_when_active_prompt_false(self):
        """
        Bug1 修复验证：activePrompt=false 时（ops-agent 等待用户输入），
        resume_event_stream 仍应连接 GET /events 消费 outbox 中残留的 interactive_request。

        场景：用户选择选项 → 页面刷新 → ops-agent 产生新 interactive_request 并等待 →
               activePrompt=false，但 outbox 有未消费事件 → 修复前：跳过 → 事件丢失
                                                              → 修复后：消费成功
        """
        from app.core.brain_port import BrainInteractiveRequest

        adapter = _make_adapter()
        state_resp = self._make_state_resp(active_prompt=False)  # activePrompt=false

        # outbox 中有一条 _ops/request_input 事件
        ir_line = _sse_line({
            "jsonrpc": "2.0",
            "id": "req-abc",
            "method": "_ops/request_input",
            "params": {
                "request": {
                    "kind": "info_request",
                    "title": "信息确认",
                    "prompt": "具体是哪台主机？",
                    "options": [{"optionId": "1", "name": "主机A"}],
                    "customInput": True,
                    "_meta": {},
                }
            },
        })
        mock_stream_resp = _make_mock_stream_resp([ir_line])

        with (
            patch.object(adapter._client, "get", new=AsyncMock(return_value=state_resp)),
            patch.object(adapter._client, "stream") as mock_stream,
        ):
            mock_stream.return_value.__aenter__ = AsyncMock(return_value=mock_stream_resp)
            mock_stream.return_value.__aexit__ = AsyncMock(return_value=False)

            events = []
            async for ev in adapter.resume_event_stream("sess-xyz"):
                events.append(ev)

        # 修复后：应该消费到 BrainInteractiveRequest
        assert len(events) == 1
        assert isinstance(events[0], BrainInteractiveRequest)
        assert events[0].prompt == "具体是哪台主机？"

    @pytest.mark.asyncio
    async def test_resume_consumes_when_active_prompt_true(self):
        """activePrompt=true（ops-agent 正在生成）时，resume 同样正常消费。"""
        adapter = _make_adapter()
        state_resp = self._make_state_resp(active_prompt=True)

        lines = [
            _sse_line(_make_agent_message_chunk("续写内容来了")),
            _sse_line(_make_session_done("end_turn")),
        ]
        mock_stream_resp = _make_mock_stream_resp(lines)

        with (
            patch.object(adapter._client, "get", new=AsyncMock(return_value=state_resp)),
            patch.object(adapter._client, "stream") as mock_stream,
        ):
            mock_stream.return_value.__aenter__ = AsyncMock(return_value=mock_stream_resp)
            mock_stream.return_value.__aexit__ = AsyncMock(return_value=False)

            events = []
            async for ev in adapter.resume_event_stream("sess-abc"):
                events.append(ev)

        assert len(events) == 1
        assert isinstance(events[0], BrainTextChunk)
        assert events[0].content == "续写内容来了"


class TestTerminateAcpPromptJsonFix:
    """Bug3 修复验证：_terminate_acp_prompt 使用 content= 而非 json=（httpx delete 不支持 json=）。"""

    @pytest.mark.asyncio
    async def test_terminate_uses_content_not_json_kwarg(self):
        """
        _terminate_acp_prompt 必须用 content= 传 JSON body，
        不能用 json=（httpx.AsyncClient.delete() 不接受 json= 参数）。

        验证：mock delete 成功收到调用，且 kwargs 中没有 json= 键。
        """
        adapter = _make_adapter()
        delete_kwargs_captured = {}

        async def mock_delete(url, **kwargs):
            delete_kwargs_captured.update(kwargs)
            resp = MagicMock(spec=httpx.Response)
            resp.status_code = 200
            resp.json.return_value = {
                "activePrompt": False,
                "pendingRequestsCancelled": 1,
                "drainedEvents": 0,
            }
            return resp

        with patch.object(adapter._client, "delete", side_effect=mock_delete):
            await adapter._terminate_acp_prompt("sess-abc", {"Content-Type": "application/json"})

        # 关键断言：不能有 json= 参数，必须有 content=
        assert "json" not in delete_kwargs_captured, (
            "httpx.AsyncClient.delete() 不支持 json= 参数，修复应使用 content="
        )
        assert "content" in delete_kwargs_captured
        # content 必须是合法 JSON bytes
        import json as _json
        payload = _json.loads(delete_kwargs_captured["content"])
        assert "reason" in payload
        assert "wait_timeout" in payload


class TestAIRegistryDefaultType:
    """Bug4 修复验证：AIAssistantRegistry 支持动态 default_type。"""

    def test_register_with_is_default_sets_default_type(self):
        """register(is_default=True) 应更新 default_type。"""
        from unittest.mock import MagicMock
        from app.services.ai_client import AIAssistantRegistry

        registry = AIAssistantRegistry()
        fake_client = MagicMock()
        registry.register("glm-4.7", fake_client, is_default=True)
        assert registry.get_default_type() == "glm-4.7"

    def test_get_default_type_falls_back_to_first_registered(self):
        """default_type 未注册时，get_default_type 返回第一个已注册类型。"""
        from unittest.mock import MagicMock
        from app.services.ai_client import AIAssistantRegistry

        registry = AIAssistantRegistry()
        # 默认 _default_type="openclaw"，不注册 openclaw，只注册 qwen3-max
        registry.register("qwen3-max", MagicMock())
        # openclaw 未注册，应降级到 qwen3-max
        assert registry.get_default_type() == "qwen3-max"

    def test_set_default_type(self):
        """set_default_type 直接设置 default_type。"""
        from unittest.mock import MagicMock
        from app.services.ai_client import AIAssistantRegistry

        registry = AIAssistantRegistry()
        registry.register("qwen3-max", MagicMock())
        registry.register("glm-4.7", MagicMock())
        registry.set_default_type("glm-4.7")
        assert registry.get_default_type() == "glm-4.7"

