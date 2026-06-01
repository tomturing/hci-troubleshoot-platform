"""
postgres 数据库管理器单元测试

覆盖 DatabaseManager.health_check 和 close 方法
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from backend.shared.database.postgres import DatabaseManager, Base


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
            mock_factory.return_value.__aenter__ = AsyncMock(
                side_effect=Exception("Connection failed")
            )
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