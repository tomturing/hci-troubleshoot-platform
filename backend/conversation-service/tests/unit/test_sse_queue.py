"""
sse_queue 模块单元测试

覆盖 QueueSSEEmitter 和 LogAuditService
"""

import asyncio
from unittest.mock import MagicMock, patch

import app.services.sse_queue as sse_queue_module
import pytest
from app.services.sse_queue import LogAuditService, QueueSSEEmitter


class TestQueueSSEEmitter:
    """QueueSSEEmitter 测试"""

    @pytest.mark.asyncio
    async def test_init_queue(self):
        """初始化时绑定队列"""
        queue = asyncio.Queue()
        emitter = QueueSSEEmitter(queue)
        assert emitter._queue is queue

    @pytest.mark.asyncio
    async def test_emit_put_data(self):
        """emit 将数据放入队列"""
        queue = asyncio.Queue()
        emitter = QueueSSEEmitter(queue)

        data = {"type": "thinking", "step": 1, "message": "test"}
        await emitter.emit("session-123", data)

        # 验证数据已放入队列
        result = await queue.get()
        assert result == data

    @pytest.mark.asyncio
    async def test_emit_multiple_events(self):
        """emit 多个事件依次入队"""
        queue = asyncio.Queue()
        emitter = QueueSSEEmitter(queue)

        events = [
            {"type": "thinking", "step": 1},
            {"type": "tool_executing", "tool": "test"},
            {"_text": "AI response"},
        ]

        for event in events:
            await emitter.emit("session-123", event)

        for expected in events:
            result = await queue.get()
            assert result == expected


class TestLogAuditService:
    """LogAuditService 测试"""

    @pytest.mark.asyncio
    async def test_write_audit_log(self):
        """write 记录审计日志"""
        service = LogAuditService()

        # Mock logger.info 以覆盖 structlog 调用
        with patch.object(sse_queue_module, "logger") as mock_logger:
            mock_logger.info = MagicMock()

            await service.write(
                audit_id="audit-001",
                session_id="session-123",
                tool_name="test_tool",
                tool_args={"arg": "value"},
                risk_level=1,
                policy="allow",
                result={"status": "success"},
                error=None,
                started_at="2026-01-01T00:00:00Z",
                completed_at="2026-01-01T00:00:01Z",
                duration_ms=1000,
                authorized_by="user-001",
                trace_id="trace-001",
            )

            # 验证 logger.info 被调用
            mock_logger.info.assert_called_once()

    @pytest.mark.asyncio
    async def test_write_audit_log_with_error(self):
        """write 记录带错误的审计日志"""
        service = LogAuditService()

        with patch.object(sse_queue_module, "logger") as mock_logger:
            mock_logger.info = MagicMock()

            await service.write(
                audit_id="audit-002",
                session_id="session-123",
                tool_name="test_tool",
                tool_args={"arg": "value"},
                risk_level=2,
                policy="deny",
                result=None,
                error="Tool execution failed",
                started_at="2026-01-01T00:00:00Z",
                completed_at="2026-01-01T00:00:01Z",
                duration_ms=1000,
            )

            mock_logger.info.assert_called_once()
