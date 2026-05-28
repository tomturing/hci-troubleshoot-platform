"""
SSE 事件推送服务 — 将 Agent 执行命令推送到前端 SSE 连接

设计依据：
  - docs/task/agent/agent工具任务清单.md T-TOOL-07

SSE 事件格式：
  event: agent_exec_command
  data: {"execId": "...", "command": "...", ...}
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from shared.observability.logger import get_logger

logger = get_logger("sse-pusher")


class SSEPusher:
    """SSE 事件推送服务。

    管理每个 conversation_id 对应的 SSE 连接队列，
    支持 agent_exec_command 等事件的推送。
    """

    def __init__(self) -> None:
        # conversation_id -> asyncio.Queue 的映射
        self._queues: dict[str, asyncio.Queue] = {}
        logger.info(event="sse_pusher_initialized", message="SSE Pusher 服务已初始化")

    def register_queue(self, conversation_id: str, queue: asyncio.Queue) -> None:
        """注册 SSE 事件队列（在 SSE 连接建立时调用）。

        Args:
            conversation_id: 会话 ID
            queue: SSE 事件队列（用于推送事件到前端）
        """
        self._queues[conversation_id] = queue
        logger.info(
            event="sse_queue_registered",
            conversation_id=conversation_id,
            queue_count=len(self._queues),
        )

    def unregister_queue(self, conversation_id: str) -> None:
        """注销 SSE 事件队列（在 SSE 连接断开时调用）。

        Args:
            conversation_id: 会话 ID
        """
        if conversation_id in self._queues:
            del self._queues[conversation_id]
            logger.info(
                event="sse_queue_unregistered",
                conversation_id=conversation_id,
                queue_count=len(self._queues),
            )

    async def push_event(
        self,
        conversation_id: str,
        event_type: str,
        data: dict[str, Any],
    ) -> bool:
        """推送 SSE 事件到指定会话的队列。

        Args:
            conversation_id: 会话 ID
            event_type: 事件类型（如 agent_exec_command）
            data: 事件数据（JSON 对象）

        Returns:
            是否成功推送（队列不存在时返回 False）
        """
        queue = self._queues.get(conversation_id)
        if queue is None:
            logger.warning(
                event="sse_push_queue_not_found",
                conversation_id=conversation_id,
                event_type=event_type,
                message="会话无活跃 SSE 连接",
            )
            return False

        # 构造 SSE 事件格式：event: <type>\ndata: <json>\n\n
        event_data = json.dumps(data, ensure_ascii=False)
        sse_message = f"event: {event_type}\ndata: {event_data}\n\n"

        # 推送到队列
        await queue.put(sse_message)

        logger.info(
            event="sse_event_pushed",
            conversation_id=conversation_id,
            event_type=event_type,
            queue_size=queue.qsize(),
        )

        return True

    def has_queue(self, conversation_id: str) -> bool:
        """检查指定会话是否有活跃的 SSE 连接。

        Args:
            conversation_id: 会话 ID

        Returns:
            是否有活跃连接
        """
        return conversation_id in self._queues

    def get_active_conversations(self) -> list[str]:
        """获取所有活跃的会话 ID。

        Returns:
            活跃会话 ID 列表
        """
        return list(self._queues.keys())
