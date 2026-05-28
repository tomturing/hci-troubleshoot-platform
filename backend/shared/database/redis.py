"""
Redis连接管理
"""

import redis.asyncio as redis


class RedisManager:
    """Redis管理器"""

    def __init__(self, redis_url: str):
        self.redis_url = redis_url
        self.client: redis.Redis | None = None

    async def connect(self):
        """连接Redis"""
        self.client = await redis.from_url(self.redis_url, encoding="utf-8", decode_responses=True, max_connections=10)

    async def close(self):
        """关闭Redis连接"""
        if self.client:
            await self.client.close()

    async def get(self, key: str) -> str | None:
        """获取值"""
        if not self.client:
            raise RuntimeError("Redis client not connected")
        return await self.client.get(key)

    async def set(self, key: str, value: str, ex: int | None = None):
        """设置值"""
        if not self.client:
            raise RuntimeError("Redis client not connected")
        await self.client.set(key, value, ex=ex)

    async def delete(self, key: str):
        """删除键"""
        if not self.client:
            raise RuntimeError("Redis client not connected")
        await self.client.delete(key)

    async def exists(self, key: str) -> bool:
        """检查键是否存在"""
        if not self.client:
            raise RuntimeError("Redis client not connected")
        return await self.client.exists(key) > 0

    async def expire(self, key: str, seconds: int):
        """设置过期时间"""
        if not self.client:
            raise RuntimeError("Redis client not connected")
        await self.client.expire(key, seconds)

    async def hset(self, name: str, key: str, value: str):
        """设置hash值"""
        if not self.client:
            raise RuntimeError("Redis client not connected")
        await self.client.hset(name, key, value)

    async def hget(self, name: str, key: str) -> str | None:
        """获取hash值"""
        if not self.client:
            raise RuntimeError("Redis client not connected")
        return await self.client.hget(name, key)

    async def hgetall(self, name: str) -> dict:
        """获取所有hash值"""
        if not self.client:
            raise RuntimeError("Redis client not connected")
        return await self.client.hgetall(name)

    async def hdel(self, name: str, *keys):
        """删除hash键"""
        if not self.client:
            raise RuntimeError("Redis client not connected")
        await self.client.hdel(name, *keys)

    async def health_check(self) -> bool:
        """PING Redis 验证可达性"""
        if not self.client:
            return False
        try:
            return await self.client.ping()
        except Exception:
            return False

    async def lpush(self, key: str, value: str) -> int:
        """向列表左侧插入元素（用于队列写入）

        Args:
            key: 列表键名
            value: 元素值

        Returns:
            插入后列表长度
        """
        if not self.client:
            raise RuntimeError("Redis client not connected")
        return await self.client.lpush(key, value)

    async def rpop(self, key: str) -> str | None:
        """从列表右侧弹出元素（用于队列读取）

        Args:
            key: 列表键名

        Returns:
            弹出的元素值，列表为空时返回 None
        """
        if not self.client:
            raise RuntimeError("Redis client not connected")
        return await self.client.rpop(key)

    async def llen(self, key: str) -> int:
        """获取列表长度

        Args:
            key: 列表键名

        Returns:
            列表长度
        """
        if not self.client:
            raise RuntimeError("Redis client not connected")
        return await self.client.llen(key)
