#!/bin/sh
# db-migrate 容器入口脚本
# 执行顺序（严格串行）：
#   0. 空库先用 Ptah Compat 创建完整 Schema；存量库跳过，避免约束先于数据清洗
#   1. migration-runner 执行数据迁移（data-migrations/，幂等，版本化管理）
#   2. ptah-compat schema apply（扩展/表/索引/函数/触发器，最终声明式差量收敛）
#
# 前置条件：
#   - 数据库用户拥有安装 vector/pgcrypto/uuid-ossp/pg_trgm extensions 的权限
#   - atlas_dev 数据库已就绪
#
# 环境变量（由 Helm Job 注入）：
#   DATABASE_URL - 目标数据库连接串
#   DEV_URL      - 独立临时数据库连接串（用于预演变更，不得指向目标库）

set -e

echo "====== HCI DB 声明式迁移 ======"
echo "目标数据库: 已配置（连接串已脱敏）"

# ── Step 0: 空库 Schema 引导 ────────────────────────────────────────────────
# 数据迁移包含对 tool_definition/system_prompt/kbd_entry 等业务表的 DML。
# 存量库必须先迁数据再收敛新约束；全新空库则必须先创建这些基础表。
CORE_SCHEMA_READY=$(psql -v ON_ERROR_STOP=1 "$DATABASE_URL" -tAc \
  "SELECT CASE WHEN to_regclass('public.tool_definition') IS NULL THEN 'false' ELSE 'true' END;")

if [ "$CORE_SCHEMA_READY" != "true" ]; then
  echo ""
  echo ">>> Step 0: 检测到全新或未完成初始化的数据库，执行 Ptah Compat Schema 引导"
  ptah-compat schema apply \
    --url "$DATABASE_URL" \
    --to "file:///desired_schema.sql" \
    --dev-url "$DEV_URL" \
    --exclude "schema_migrations,alembic_version,atlas_schema_revisions" \
    --auto-approve
  echo "✅ Step 0 完成（数据迁移依赖表已就绪）"
else
  echo ""
  echo ">>> Step 0: 核心 Schema 已存在，保持存量库先迁数据的升级顺序"
fi

# ── Step 1: 执行数据迁移 ────────────────────────────────────────────────────
echo ""
echo ">>> Step 1: 执行数据迁移（migration-runner）"
/migration-runner.sh
echo "✅ Step 1 完成"

# ── Step 2: Ptah Compat 声明式 Schema 最终收敛 ───────────────────────────────
echo ""
echo ">>> Step 2: ptah-compat schema apply（扩展/表/索引/函数/触发器）"
ptah-compat schema apply \
  --url "$DATABASE_URL" \
  --to "file:///desired_schema.sql" \
  --dev-url "$DEV_URL" \
  --exclude "schema_migrations,alembic_version,atlas_schema_revisions" \
  --auto-approve
echo "✅ Step 2 完成"

echo ""
echo "====== 迁移完成 ======"
