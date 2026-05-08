"""
OpsAgentBrainAdapter._consume_events 单元测试

覆盖 2026-05 修复：session/done 到达但无文本产出时触发 BrainUnavailableError（fallback）。
"""
import json
from unittest.mock import AsyncMock, MagicMock, patch

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
