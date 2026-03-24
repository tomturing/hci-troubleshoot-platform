"""
SSE 事件队列——将 ReactExecutor 内部事件桥接到 SSE 响应流

QueueSSEEmitter 将 SSEEmitterProtocol 的 emit() 调用转换为
asyncio.Queue 插入，供路由层的 event_generator 消费。

队列消息格式：
  {"type": "thinking", "step": 1, "message": "..."}
  {"type": "confirm_request", "tool_name": "...", ...}
  {"type": "tool_executing", "tool": "...", "args": {...}}
  {"_text": "AI 回复文本片段"}   ← 文本块使用 _text 键
  None                           ← 哨兵，表示流结束
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)


class QueueSSEEmitter:
    """
    基于 asyncio.Queue 的 SSE 事件发射器。
    ReactExecutor 通过此对象向队列推送事件，路由层消费后格式化为 SSE。
    """

    def __init__(self, queue: asyncio.Queue) -> None:
        self._queue = queue

    async def emit(self, session_id: str, data: dict) -> None:
        """将 SSE 事件放入队列（非阻塞，队列无限大）"""
        await self._queue.put(data)
        logger.debug(f"SSE emit [session={session_id}] type={data.get('type')}")


class LogAuditService:
    """
    简单的审计日志服务——将工具调用审计记录写到结构化日志。
    Phase 4 MVP 版本，后续可替换为写入 tool_audit_log 数据库表。
    """

    async def write(
        self,
        id: str,
        session_id: str,
        tool_name: str,
        tool_args: dict,
        result: Any,
        error: str | None,
        started_at: Any,
        completed_at: Any,
        duration_ms: int,
        authorized_by: str | None = None,
    ) -> None:
        """记录工具调用审计日志"""
        logger.info(
            event="tool_audit",
            audit_id=id,
            session_id=session_id,
            tool_name=tool_name,
            duration_ms=duration_ms,
            has_error=error is not None,
            authorized_by=authorized_by,
        )
