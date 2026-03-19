"""Alembic 迁移环境配置

此文件由 alembic init 生成，已修改为：
1. 从环境变量 DATABASE_URL 读取数据库连接串
2. 使用 asyncpg 驱动（与应用层保持一致）
3. 自动导入所有 ORM 模型（支持 autogenerate）
"""

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

# 导入所有 ORM 模型，确保 autogenerate 可发现所有表结构
from backend.shared.database.postgres import Base  # noqa: F401
import backend.shared.models.kb  # noqa: F401
import backend.shared.models.schemas  # noqa: F401
import backend.shared.models.user  # noqa: F401

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# autogenerate 的目标元数据（必须包含所有 ORM 模型的表）
target_metadata = Base.metadata

# 从环境变量读取数据库 URL；本地开发使用默认值（与 docker-compose 一致）
DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+asyncpg://hci_admin:dev_password_123@localhost:5432/hci_troubleshoot",
)


def run_migrations_offline() -> None:
    """离线模式：生成 SQL 脚本，不需要实际数据库连接。

    适用于：代码审查、预测迁移 SQL、CI 离线验证。
    """
    context.configure(
        url=DATABASE_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    """在给定连接上执行迁移（被 run_migrations_online 调用）。"""
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """在线模式：连接数据库执行迁移。

    适用于：本地开发迁移、CI/CD 集成测试、生产部署。
    """
    engine = create_async_engine(DATABASE_URL, echo=False)
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
