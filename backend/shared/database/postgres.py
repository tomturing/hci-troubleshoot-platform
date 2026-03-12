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
        self.engine = create_async_engine(
            database_url,
            echo=False,
            pool_size=20,
            max_overflow=10,
            pool_pre_ping=True,
            # 连接级别设置：空闲事务超时 30 秒，防止 CancelledError 后连接以
            # idle-in-transaction 状态滞留连接池，阻塞后续 INSERT（经由触发器锁链）
            connect_args={
                "server_settings": {
                    "idle_in_transaction_session_timeout": "30000",  # 单位 ms
                }
            },
        )
        self.async_session_factory = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)

    async def get_session(self) -> AsyncGenerator[AsyncSession, None]:
        """获取数据库会话"""
        async with self.async_session_factory() as session:
            try:
                yield session
                await session.commit()
            except BaseException:
                # 使用 BaseException 确保 asyncio.CancelledError 也能触发回滚
                # 避免异步取消导致事务以 idle-in-transaction 状态阻塞后续写入
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
