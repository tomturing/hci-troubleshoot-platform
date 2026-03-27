"""
对话工具 - 用户交互相关工具

包括 ask_user 工具：向用户提问并等待回复
"""

import json

from redis.asyncio import Redis
from shared.utils.logger import get_logger

logger = get_logger("dialog-tools")

# 等待用户回复的超时秒数
ASK_USER_TIMEOUT = 300  # 5 分钟
REDIS_KEY_PREFIX = "ask_user:"


class DialogTools:
    """对话工具集"""

    def __init__(
        self,
        redis: Redis | None = None,
        confirm_service=None,  # ConfirmService 实例
        sse_emitter=None,        # SSE 发射器
    ):
        """
        Args:
            redis: Redis 客户端（用于存储问题）
            confirm_service: 确认服务实例（用于等待用户回复）
            sse_emitter: SSE 发射器（用于推送事件）
        """
        self._redis = redis
        self._confirm_service = confirm_service
        self._sse_emitter = sse_emitter

    async def ask_user(
        self,
        session_id: str,
        question: str,
        options: list[str] | None = None,
    ) -> str | None:
        """
        向用户提问并等待回复

        工作流程：
        1. 通过 SSE 推送 user_question 事件给前端
        2. 将问题写入 Redis
        3. 等待用户通过 confirm_service.submit_confirm 提交回复
        4. 返回用户回复内容

        Args:
            session_id: 会话 ID
            question: 问题内容
            options: 可选，预设选项列表

        Returns:
            用户回复内容，若超时或取消则返回 None
        """
        if not self._redis or not self._confirm_service:
            logger.warning(
                event="ask_user_unavailable",
                message="Redis 或 ConfirmService 未配置",
                session_id=session_id,
            )
            return None

        # 1. 通过 SSE 推送 user_question 事件
        if self._sse_emitter:
            event_data = {
                "type": "user_question",
                "question": question,
                "options": options,
                "session_id": session_id,
            }
            await self._sse_emitter.emit("user_question", event_data)
            logger.info(
                event="ask_user_sent",
                message="已推送 user_question 事件",
                session_id=session_id,
                question=question[:100],
            )

        # 2. 将问题写入 Redis（使用 confirm_service 的机制等待回复）
        key = f"{REDIS_KEY_PREFIX}{session_id}"
        await self._redis.delete(key)

        payload = {"question": question, "options": options}
        await self._redis.set(key, json.dumps(payload), ex=ASK_USER_TIMEOUT)

        logger.info(
            event="ask_user_waiting",
            message="等待用户回复",
            session_id=session_id,
            question=question[:100],
            timeout=ASK_USER_TIMEOUT,
        )

        # 3. 等待用户回复（复用 confirm_service 的 BRPOP 机制）
        # 注意：前端调用 /confirm 接口提交回复时，会同时写入 confirm:{session_id}
        # 这里复用该机制，区别是 key 前缀不同
        try:
            confirmed = await self._confirm_service.request_confirm(
                session_id=session_id,
                tool_name="ask_user",
                tool_args={"question": question},
                risk_level=1,  # 提问无需授权
            )

            if not confirmed:
                logger.info(
                    event="ask_user_cancelled",
                    message="用户取消或超时",
                    session_id=session_id,
                )
                return None

            # 获取用户回复内容
            # 前端应该在 submit_confirm 时将回复内容写入 Redis
            reply_key = f"{REDIS_KEY_PREFIX}reply:{session_id}"
            reply_data = await self._redis.get(reply_key)
            if reply_data:
                reply = json.loads(reply_data)
                user_reply = reply.get("reply", "")
                logger.info(
                    event="ask_user_received",
                    message="收到用户回复",
                    session_id=session_id,
                    reply=user_reply[:100] if user_reply else "",
                )
                return user_reply

            # 如果没有专门存储回复，视为用户确认（简单场景）
            return "confirmed"

        except Exception as e:
            logger.error(
                event="ask_user_error",
                message=str(e),
                session_id=session_id,
            )
            return None


# ask_user 工具定义（用于注册到 tool_registry）
ASK_USER_TOOL_DEFINITION = {
    "name": "ask_user",
    "description": (
        "向用户提问并等待回复。当需要收集更多信息、确认故障细节、或让用户做选择时使用。"
        "该工具会暂停执行直到用户回复。"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "需要询问用户的问题内容",
            },
            "options": {
                "type": "array",
                "items": {"type": "string"},
                "description": "可选，预设选项列表",
            },
        },
        "required": ["question"],
    },
    "risk_level": 1,
    "policy": "auto",  # 提问自动执行
    "category": "dialog",
}
