"""
PostgreSQL数据库连接管理
"""

from collections.abc import AsyncGenerator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class DatabaseManager:
    """数据库管理器"""

    def __init__(self, database_url: str):
        self.engine = create_async_engine(database_url, echo=False, pool_size=20, max_overflow=10, pool_pre_ping=True)
        self.async_session_factory = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)

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

    async def health_check(self) -> bool:
        """执行 SELECT 1 验证数据库可达性"""
        try:
            async with self.async_session_factory() as session:
                await session.execute(text("SELECT 1"))
            return True
        except Exception:
            return False

    async def close(self):
        """关闭数据库连接"""
        await self.engine.dispose()
