"""
人工确认服务——通过 Redis BRPOP 实现 ReAct 执行器的阻塞等待确认

工作流：
  1. ReactExecutor 调用 request_confirm()，阻塞等待 Redis key
  2. 同时通过 SSE 推送 confirm_request 事件给前端（在 ReactExecutor 中完成）
  3. 前端收到事件后展示确认弹窗，用户点击确认/取消
  4. 前端调用 POST /api/conversations/{session_id}/confirm 接口
  5. submit_confirm() 向 Redis key LPUSH 确认结果
  6. request_confirm() 的 BRPOP 返回，ReAct 执行器继续运行

Redis Key 设计：confirm:{session_id}（LIST 类型，BRPOP 等待，LPUSH 写入）
安全边界：Redis 不可用时，调用方应 fallback 为 block
"""

import json
import logging
from enum import Enum

from redis.asyncio import Redis

logger = logging.getLogger(__name__)

# 等待用户确认的超时秒数（120s 后自动取消）
CONFIRM_TIMEOUT = 120
REDIS_KEY_PREFIX = "confirm:"


class ConfirmResult(str, Enum):
    """确认结果枚举，区分超时和用户拒绝"""

    APPROVED = "approved"      # 用户确认执行
    REJECTED = "rejected"      # 用户主动拒绝
    TIMEOUT = "timeout"        # 等待超时，自动取消


class ConfirmService:
    """人工确认服务（基于 Redis BRPOP 异步等待）"""

    def __init__(self, redis: Redis):
        self.redis = redis

    async def request_confirm(
        self,
        session_id: str,
        tool_name: str,
        tool_args: dict,
        risk_level: int,
    ) -> ConfirmResult:
        """
        请求用户确认，阻塞等待直到用户响应或超时（120s）。

        返回值：
          APPROVED = 用户点击"确认"
          REJECTED = 用户点击"取消"
          TIMEOUT  = 等待超时，自动取消
        """
        key = f"{REDIS_KEY_PREFIX}{session_id}"

        # 清空可能残留的上一次确认结果（保证幂等）
        await self.redis.delete(key)

        logger.info(
            f"等待用户确认 [session={session_id}] "
            f"工具={tool_name} risk_level={risk_level}，超时={CONFIRM_TIMEOUT}s"
        )

        # BRPOP 阻塞等待，timeout=0 表示永久等待，这里设置超时
        result = await self.redis.brpop(key, timeout=CONFIRM_TIMEOUT)

        if result is None:
            logger.warning(f"用户确认超时 [session={session_id}] 工具={tool_name}")
            return ConfirmResult.TIMEOUT

        _, value = result
        try:
            data = json.loads(value)
            confirmed: bool = bool(data.get("confirmed", False))
            result_type = ConfirmResult.APPROVED if confirmed else ConfirmResult.REJECTED
            logger.info(
                f"用户确认结果 [session={session_id}] 工具={tool_name}: {result_type.value}"
            )
            return result_type
        except Exception as e:
            logger.error(f"解析确认结果失败 [session={session_id}]: {e}")
            return ConfirmResult.REJECTED

    async def submit_confirm(
        self,
        session_id: str,
        confirmed: bool,
        authorized_by: str,
    ) -> None:
        """
        提交用户确认结果（由 POST /confirm 路由调用）。

        向 Redis 的 confirm:{session_id} key LPUSH 确认结果，
        解除 request_confirm() 的 BRPOP 等待。
        """
        key = f"{REDIS_KEY_PREFIX}{session_id}"
        value = json.dumps({"confirmed": confirmed, "authorized_by": authorized_by})
        await self.redis.lpush(key, value)
        # 设置过期防止遗留数据堆积（5 分钟）
        await self.redis.expire(key, 300)
        logger.info(
            f"已提交确认结果 [session={session_id}] confirmed={confirmed} by={authorized_by}"
        )
