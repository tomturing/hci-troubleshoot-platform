#!/bin/sh
# db-migrate 容器入口脚本
# 执行顺序（严格串行）：
#   1. psql 应用函数（幂等，可在表创建前运行）
#   2. atlas schema apply（声明式，diff 计算后执行 ALTER/CREATE/DROP）
#   3. psql 应用触发器（需要表已存在）
#
# 前置条件（由 K8s initContainer 确保，脚本不重复处理）：
#   - 目标库已安装 vector/pgcrypto/uuid-ossp/pg_trgm extensions
#   - atlas_dev 数据库已就绪
#
# 环境变量（由 Helm Job 注入）：
#   DATABASE_URL - 目标数据库连接串
#   DEV_URL      - Atlas dev 数据库连接串（空数据库，用于 SQL 规范化）

set -e

echo "====== HCI DB 声明式迁移 ======"
echo "目标数据库: ${DATABASE_URL%%\?*}"

# ── Step 1: 应用函数 ───────────────────────────────────────────────────────
echo ""
echo ">>> Step 1: 应用函数（CREATE OR REPLACE FUNCTION）"
psql -v ON_ERROR_STOP=1 "$DATABASE_URL" -f /desired_extras.sql
echo "✅ Step 1 完成"

# ── Step 2: Atlas 声明式 Schema 应用 ──────────────────────────────────────
echo ""
echo ">>> Step 2: Atlas schema apply（声明式差量同步）"
atlas schema apply \
  --url "$DATABASE_URL" \
  --to "file:///desired_schema.sql" \
  --dev-url "$DEV_URL" \
  --exclude "schema_migrations,alembic_version,atlas_schema_revisions" \
  --auto-approve
echo "✅ Step 2 完成"

# ── Step 3: 应用触发器 ────────────────────────────────────────────────────
# 触发器依赖表结构，必须在 Atlas 创建/更新表后执行
echo ""
echo ">>> Step 3: 重建触发器（DROP TRIGGER IF EXISTS + CREATE TRIGGER）"
psql -v ON_ERROR_STOP=1 "$DATABASE_URL" -f /desired_extras.sql
echo "✅ Step 3 完成（触发器已重建）"

echo ""
echo "====== 迁移完成 ======"
