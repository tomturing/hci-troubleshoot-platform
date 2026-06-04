"""
postgres 数据库管理器单元测试

覆盖 DatabaseManager.health_check, get_session 和 close 方法
"""

import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.shared.database.postgres import Base, DatabaseManager


class TestDatabaseManager:
    """DatabaseManager 测试"""

    @pytest.fixture
    def db_manager(self):
        """创建 DatabaseManager 实例"""
        return DatabaseManager("postgresql+asyncpg://test:test@localhost/test")

    def test_base_declarative(self):
        """验证 Base 是 declarative_base"""
        assert Base is not None
        assert hasattr(Base, "registry")

    @pytest.mark.asyncio
    async def test_get_session_commit_success(self, db_manager):
        """get_session 正常提交路径"""
        mock_session = AsyncMock()
        mock_session.commit = AsyncMock()
        mock_session.close = AsyncMock()

        with patch.object(db_manager, "async_session_factory") as mock_factory:
            mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_factory.return_value.__aexit__ = AsyncMock()

            # 模拟生成器消费
            gen = db_manager.get_session()
            session = await gen.__anext__()
            assert session is mock_session

            # 模拟正常退出（触发 commit）
            with contextlib.suppress(StopAsyncIteration):
                await gen.__anext__()

            mock_session.commit.assert_called_once()
            mock_session.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_health_check_success(self, db_manager):
        """health_check 成功返回 True"""
        # Mock session execute
        mock_session = AsyncMock()
        mock_session.execute = AsyncMock()

        with patch.object(db_manager, "async_session_factory") as mock_factory:
            mock_factory.return_value.__aenter__ = AsyncMock(return_value=mock_session)
            mock_factory.return_value.__aexit__ = AsyncMock()

            result = await db_manager.health_check()
            assert result is True

    @pytest.mark.asyncio
    async def test_health_check_failure(self, db_manager):
        """health_check 失败返回 False"""
        with patch.object(db_manager, "async_session_factory") as mock_factory:
            mock_factory.return_value.__aenter__ = AsyncMock(side_effect=Exception("Connection failed"))
            mock_factory.return_value.__aexit__ = AsyncMock()

            result = await db_manager.health_check()
            assert result is False

    @pytest.mark.asyncio
    async def test_close(self, db_manager):
        """close 关闭数据库连接"""
        mock_engine = MagicMock()
        mock_engine.dispose = AsyncMock()
        db_manager.engine = mock_engine

        await db_manager.close()
        mock_engine.dispose.assert_called_once()
