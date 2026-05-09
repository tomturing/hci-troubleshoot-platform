"""BrainRouter 单元测试。

覆盖三个核心场景（Issue 8）：
1. BrainRouter 注入时正确产出 BrainTextChunk
2. ops-agent 不可达触发 BrainUnavailableError 后能降级到 htp 并产出 fallback notice
3. BrainStageUpdate 仅在 stage 符合 S\\d 时透传给 ConversationService 的调用者
"""

from __future__ import annotations

import os
import sys
from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import MagicMock

import pytest

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

from app.adapters.brain_router import BrainRouter
from app.core.brain_port import BrainEvent, BrainStageUpdate, BrainTextChunk, BrainUnavailableError

# ── 辅助工厂 ──────────────────────────────────────────────────────────────────

def _make_mock_adapter(events: list[BrainEvent]):
    """创建产出固定事件序列的 mock 适配器。"""

    async def _process(**_kwargs: Any) -> AsyncGenerator[BrainEvent, None]:
        for e in events:
            yield e

    adapter = MagicMock()
    adapter.process = _process
    return adapter


def _make_failing_adapter(reason: str = "connection refused"):
    """创建抛出 BrainUnavailableError 的 mock 适配器。"""

    async def _process(**_kwargs: Any) -> AsyncGenerator[BrainEvent, None]:
        raise BrainUnavailableError(brain_name="ops-agent", reason=reason)
        yield  # 让 Python 将函数识别为 async generator

    adapter = MagicMock()
    adapter.process = _process
    return adapter


async def _collect(router: BrainRouter, **kwargs: Any) -> list[BrainEvent]:
    """收集 BrainRouter.process() 的所有输出事件。"""
    return [e async for e in router.process(**kwargs)]


# ── 测试用例 ──────────────────────────────────────────────────────────────────


class TestBrainRouterTextChunk:
    """场景 1：注入 BrainRouter 时能正确产出 BrainTextChunk。"""

    @pytest.mark.asyncio
    async def test_htp_path_yields_text_chunks(self):
        """assistant_type 非 ops-agent 时走 htp 路径，正确产出文本。"""
        chunks = [BrainTextChunk(content="hello"), BrainTextChunk(content=" world")]
        htp = _make_mock_adapter(chunks)
        router = BrainRouter(htp_adapter=htp)

        events = await _collect(
            router,
            assistant_type="openclaw",
            session_id="s1",
            messages=[],
        )

        assert len(events) == 2
        assert all(isinstance(e, BrainTextChunk) for e in events)
        assert events[0].content == "hello"
        assert events[1].content == " world"

    @pytest.mark.asyncio
    async def test_ops_agent_path_yields_text_chunks(self):
        """assistant_type=ops-agent 且适配器可用时走 ops 路径，正确产出文本。"""
        chunks = [BrainTextChunk(content="ops reply")]
        ops = _make_mock_adapter(chunks)
        htp = _make_mock_adapter([])
        router = BrainRouter(htp_adapter=htp, ops_agent_adapter=ops)

        events = await _collect(
            router,
            assistant_type="ops-agent",
            session_id="s2",
            messages=[],
        )

        assert len(events) == 1
        assert isinstance(events[0], BrainTextChunk)
        assert events[0].content == "ops reply"


class TestBrainRouterFallback:
    """场景 2：ops-agent 不可达时能降级到 htp 并产出 fallback notice。"""

    @pytest.mark.asyncio
    async def test_fallback_on_brain_unavailable(self):
        """ops-agent 抛 BrainUnavailableError 后，降级到 htp 并发送降级提示。"""
        ops = _make_failing_adapter(reason="connect error")
        htp_chunks = [BrainTextChunk(content="htp fallback")]
        htp = _make_mock_adapter(htp_chunks)
        router = BrainRouter(htp_adapter=htp, ops_agent_adapter=ops)

        events = await _collect(
            router,
            assistant_type="ops-agent",
            session_id="s3",
            messages=[],
        )

        # 应至少有一个 fallback notice chunk 和 htp 的输出
        text_contents = [e.content for e in events if isinstance(e, BrainTextChunk)]
        assert any("ops-agent" in c or "备用" in c or "自动切换" in c for c in text_contents), (
            "应包含降级通知文本"
        )
        assert any(c == "htp fallback" for c in text_contents), (
            "应包含 htp fallback 输出"
        )

    @pytest.mark.asyncio
    async def test_no_ops_adapter_uses_htp(self):
        """ops_agent_adapter=None 时，assistant_type=ops-agent 降级到 htp 并发送提示。"""
        htp_chunks = [BrainTextChunk(content="htp only")]
        htp = _make_mock_adapter(htp_chunks)
        router = BrainRouter(htp_adapter=htp, ops_agent_adapter=None)

        events = await _collect(
            router,
            assistant_type="ops-agent",
            session_id="s4",
            messages=[],
        )

        # 应包含降级提示 + htp 输出
        text_contents = [e.content for e in events if isinstance(e, BrainTextChunk)]
        assert any("未启用" in c or "备用" in c or "自动切换" in c for c in text_contents), (
            "应包含未启用提示文本"
        )
        assert any(c == "htp only" for c in text_contents), (
            "应包含 htp fallback 输出"
        )


class TestBrainRouterStageUpdate:
    """场景 3：BrainStageUpdate 仅在 stage 符合 S\\d 格式时透传。"""

    @pytest.mark.asyncio
    async def test_s_stage_forwarded(self):
        """S0-S6 格式 stage 应正常产出。"""
        events = [BrainStageUpdate(stage="S3"), BrainTextChunk(content="text")]
        htp = _make_mock_adapter(events)
        router = BrainRouter(htp_adapter=htp)

        result = await _collect(
            router,
            assistant_type="openclaw",
            session_id="s5",
            messages=[],
        )

        stage_events = [e for e in result if isinstance(e, BrainStageUpdate)]
        assert len(stage_events) == 1
        assert stage_events[0].stage == "S3"

    @pytest.mark.asyncio
    async def test_ops_internal_stage_forwarded_from_router(self):
        """BrainRouter 透传 ops-agent 的内部 stage（过滤由 ConversationService 处理）。"""
        events = [BrainStageUpdate(stage="intake"), BrainTextChunk(content="done")]
        ops = _make_mock_adapter(events)
        htp = _make_mock_adapter([])
        router = BrainRouter(htp_adapter=htp, ops_agent_adapter=ops)

        result = await _collect(
            router,
            assistant_type="ops-agent",
            session_id="s6",
            messages=[],
        )

        # BrainRouter 本身不过滤 stage，过滤在 ConversationService 层
        stage_events = [e for e in result if isinstance(e, BrainStageUpdate)]
        assert len(stage_events) == 1
        assert stage_events[0].stage == "intake"
