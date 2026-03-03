"""
Session Management Service - WebSocket会话管理
"""

import json
from datetime import datetime

from shared.database.redis import RedisManager
from shared.utils.logger import get_logger

logger = get_logger("session-service")


class SessionManager:
    """会话管理器"""

    def __init__(self, redis_manager: RedisManager):
        self.redis = redis_manager
        self.active_connections: dict[str, any] = {}

    async def create_session(self, client_id: str, websocket: any, case_id: str | None = None) -> str:
        """创建会话"""
        session_key = f"session:{client_id}"

        session_data = {"client_id": client_id, "case_id": case_id or "", "connected_at": str(datetime.utcnow())}

        await self.redis.set(
            session_key,
            json.dumps(session_data),
            ex=86400,  # 24小时过期
        )

        self.active_connections[client_id] = websocket

        logger.info(
            event="session_created",
            message=f"Session created for client {client_id}",
            client_id=client_id,
            case_id=case_id,
        )

        return session_key

    async def get_session(self, client_id: str) -> dict | None:
        """获取会话"""
        session_key = f"session:{client_id}"
        session_data = await self.redis.get(session_key)

        if not session_data:
            return None

        return json.loads(session_data)

    async def close_session(self, client_id: str):
        """关闭会话"""
        session_key = f"session:{client_id}"
        await self.redis.delete(session_key)

        if client_id in self.active_connections:
            del self.active_connections[client_id]

        logger.info(event="session_closed", message=f"Session closed for client {client_id}", client_id=client_id)

    def get_connection(self, client_id: str):
        """获取WebSocket连接"""
        return self.active_connections.get(client_id)
