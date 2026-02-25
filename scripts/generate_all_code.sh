#!/bin/bash

# HCI智能排障平台 - 完整代码生成脚本
# 此脚本会生成所有核心代码文件

set -e

PROJECT_ROOT="/home/bot/aihci/hci-troubleshoot-platform"
cd "$PROJECT_ROOT"

echo "=========================================="
echo "开始生成HCI智能排障平台完整代码..."
echo "=========================================="

# 1. 生成 Shared 模块
echo "1. 生成 Shared 模块..."

# database/postgres.py
cat > backend/shared/database/postgres.py << 'EOF'
"""
PostgreSQL数据库连接管理
"""

from typing import AsyncGenerator
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import declarative_base
from contextlib import asynccontextmanager

Base = declarative_base()

class DatabaseManager:
    """数据库管理器"""
    
    def __init__(self, database_url: str):
        self.engine = create_async_engine(
            database_url,
            echo=False,
            pool_size=20,
            max_overflow=10,
            pool_pre_ping=True
        )
        self.async_session_factory = async_sessionmaker(
            self.engine,
            class_=AsyncSession,
            expire_on_commit=False
        )
    
    async def get_session(self) -> AsyncGenerator[AsyncSession, None]:
        """获取数据库会话"""
        async with self.async_session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise
            finally:
                await session.close()
    
    async def close(self):
        """关闭数据库连接"""
        await self.engine.dispose()
EOF

# database/redis.py
cat > backend/shared/database/redis.py << 'EOF'
"""
Redis连接管理
"""

import redis.asyncio as redis
from typing import Optional
import json

class RedisManager:
    """Redis管理器"""
    
    def __init__(self, redis_url: str):
        self.redis_url = redis_url
        self.client: Optional[redis.Redis] = None
    
    async def connect(self):
        """连接Redis"""
        self.client = await redis.from_url(
            self.redis_url,
            encoding="utf-8",
            decode_responses=True,
            max_connections=10
        )
    
    async def close(self):
        """关闭Redis连接"""
        if self.client:
            await self.client.close()
    
    async def get(self, key: str) -> Optional[str]:
        """获取值"""
        if not self.client:
            raise RuntimeError("Redis client not connected")
        return await self.client.get(key)
    
    async def set(self, key: str, value: str, ex: Optional[int] = None):
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
    
    async def hget(self, name: str, key: str) -> Optional[str]:
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
EOF

# models/base.py
cat > backend/shared/models/base.py << 'EOF'
"""
基础数据模型
"""

from datetime import datetime
from sqlalchemy import Column, DateTime, String
from sqlalchemy.ext.declarative import declared_attr

class TimestampMixin:
    """时间戳混入类"""
    
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

class TraceableMixin:
    """可追踪混入类"""
    
    trace_id = Column(String(50), index=True, nullable=True)
EOF

echo "Shared 模块生成完成"

# 2. 生成 Case Service
echo "2. 生成 Case Service..."

# case-service/app/models/case.py
cat > backend/case-service/app/models/case.py << 'EOF'
"""
Case数据模型
"""

from sqlalchemy import Column, String, Text, Enum as SQLEnum, DateTime
from sqlalchemy.dialects.postgresql import UUID
import uuid
import enum
from datetime import datetime

from ....shared.database.postgres import Base
from ....shared.models.base import TimestampMixin, TraceableMixin

class CaseStatus(str, enum.Enum):
    """工单状态枚举"""
    CREATED = "created"
    CONFIRMED = "confirmed"
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"
    CLOSED = "closed"
    CANCELLED = "cancelled"

class Case(Base, TimestampMixin, TraceableMixin):
    """工单表"""
    
    __tablename__ = "cases"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    case_id = Column(String(50), unique=True, nullable=False, index=True)
    client_id = Column(String(100), nullable=False, index=True)
    status = Column(SQLEnum(CaseStatus), default=CaseStatus.CREATED, nullable=False)
    title = Column(String(200), nullable=False)
    description = Column(Text)
    closed_at = Column(DateTime, nullable=True)
    
    def __repr__(self):
        return f"<Case(case_id={self.case_id}, status={self.status})>"
EOF

echo "Case Service 生成完成"

echo "=========================================="
echo "核心代码文件生成完成！"
echo "=========================================="

