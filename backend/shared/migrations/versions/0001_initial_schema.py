"""初始化数据库 Schema（历史基线）

本迁移为**空操作基准版本**，标记现有 Schema（由 database/init_schema.sql 手动创建）
已成为 Alembic 版本历史的起点。

新环境部署流程：
  1. psql -U hci_admin -d hci_troubleshoot -f database/init_schema.sql
  2. export DATABASE_URL=postgresql+asyncpg://hci_admin:...@host:5432/hci_troubleshoot
  3. uv run alembic stamp head   # 标记 Schema 为当前版本，跳过此迁移
  
已有数据库升级流程：
  uv run alembic upgrade head   # 应用此版本之后的所有增量迁移

历史变更记录（已作为 SQL 文件存档）：
  - database/init_schema.sql          — 初始完整 Schema
  - database/migrate_evaluation_v1.sql — 评估模块 v1
  - database/migrate_kb_v3.sql        — 知识库模块 v3

Revision ID: 0001
Revises: (无)
Create Date: 2026-03-19
"""

from alembic import op  # noqa: F401

# revision identifiers
revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 初始 Schema 通过 database/init_schema.sql 手动执行
    # 已运行的数据库执行 `alembic stamp head` 而非此迁移
    pass


def downgrade() -> None:
    # 不提供降级：初始 Schema 的回滚等同于清空整个数据库
    # 如需重置，请直接 DROP DATABASE 并重新执行 init_schema.sql
    pass
