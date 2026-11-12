#!/bin/sh
# db-migrate 容器入口脚本
# 执行顺序（严格串行）：
#   0. 空库先用 Atlas 创建基础表；存量库跳过，避免约束先于数据清洗
#   1. migration-runner 执行数据迁移（data-migrations/，幂等，版本化管理）
#   2. psql 应用函数（desired_extras.sql，幂等：CREATE OR REPLACE FUNCTION）
#   3. atlas schema apply（最终声明式差量收敛）
#   4. psql 应用触发器（需要表已存在，幂等：DROP TRIGGER IF EXISTS + CREATE TRIGGER）
#   5. psql 按文件名顺序执行种子数据（seeds/，幂等：ON CONFLICT DO NOTHING）
#
# 为什么 Step 5 不能提前、也不能只靠 Helm 的 PostSync db-seed：
#   - 种子描述的是「本次 Schema 收敛后的初始/参考数据」，必须晚于 Step 3；
#   - data-migrations 在存量库跑在 Step 3 **之前**，所以新表的种子不能放那里
#     （2026-10-29 staging 事故：056 报 relation does not exist）；
#   - PostSync db-seed 仍然保留（Helm 直装与兼容路径），两边重复执行一次，
#     所有种子均幂等（02/03/06 DO NOTHING 保护运营改动，01 DO UPDATE 是代码强约束）。
#
# 前置条件：
#   - 数据库用户拥有安装 vector/pgcrypto/uuid-ossp/pg_trgm extensions 的权限
#   - atlas_dev 数据库已就绪
#
# 环境变量（由 Helm Job 注入）：
#   DATABASE_URL - 目标数据库连接串
#   DEV_URL      - Atlas dev 数据库连接串（空数据库，用于 SQL 规范化）

set -e

echo "====== HCI DB 声明式迁移 ======"
echo "目标数据库: 已配置（连接串已脱敏）"

# 目标库的类型与函数扩展是 desired_schema.sql 的硬依赖。迁移入口自行确保，
# 避免 Docker Compose、Helm 和 CI 对扩展初始化顺序产生隐式差异。
echo ">>> 确保目标数据库扩展对象"
psql -v ON_ERROR_STOP=1 "$DATABASE_URL" <<'SQL'
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS pg_trgm;
SQL

# Atlas 在规范化文件 Schema 时会清理 dev 数据库的 public schema。pgvector 等扩展
# 记录可能仍存在，但 vector 类型/函数已随 schema 被删除；下一次 apply 前必须重建。
prepare_atlas_dev() {
  echo ">>> 重置 Atlas dev 数据库扩展对象"
  psql -v ON_ERROR_STOP=1 "$DEV_URL" <<'SQL'
CREATE SCHEMA IF NOT EXISTS public;
DROP EXTENSION IF EXISTS vector CASCADE;
DROP EXTENSION IF EXISTS pgcrypto CASCADE;
DROP EXTENSION IF EXISTS "uuid-ossp" CASCADE;
DROP EXTENSION IF EXISTS pg_trgm CASCADE;
CREATE EXTENSION vector;
CREATE EXTENSION pgcrypto;
CREATE EXTENSION "uuid-ossp";
CREATE EXTENSION pg_trgm;
SQL
}

# ── Step 0: 空库 Schema 引导 ────────────────────────────────────────────────
# 数据迁移包含对 tool_definition/system_prompt/kbd_entry 等业务表的 DML。
# 存量库必须先迁数据再收敛新约束；全新空库则必须先创建这些基础表。
CORE_SCHEMA_READY=$(psql -v ON_ERROR_STOP=1 "$DATABASE_URL" -tAc \
  "SELECT CASE WHEN to_regclass('public.tool_definition') IS NULL THEN 'false' ELSE 'true' END;")

if [ "$CORE_SCHEMA_READY" != "true" ]; then
  echo ""
  echo ">>> Step 0: 检测到全新或未完成初始化的数据库，执行 Atlas 基础 Schema 引导"
  prepare_atlas_dev
  atlas schema apply \
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

# ── Step 2: 应用函数 ────────────────────────────────────────────────────────
echo ""
echo ">>> Step 2: 应用函数（CREATE OR REPLACE FUNCTION）"
psql -v ON_ERROR_STOP=1 "$DATABASE_URL" -f /desired_extras.sql
echo "✅ Step 2 完成"

# ── Step 3: Atlas 声明式 Schema 最终收敛 ────────────────────────────────────
echo ""
echo ">>> Step 3: Atlas schema apply（最终声明式差量收敛）"
prepare_atlas_dev
atlas schema apply \
  --url "$DATABASE_URL" \
  --to "file:///desired_schema.sql" \
  --dev-url "$DEV_URL" \
  --exclude "schema_migrations,alembic_version,atlas_schema_revisions" \
  --auto-approve
echo "✅ Step 3 完成"

# ── Step 4: 应用触发器 ────────────────────────────────────────────────────
# 触发器依赖表结构，必须在 Atlas 创建/更新表后执行
echo ""
echo ">>> Step 4: 重建触发器（DROP TRIGGER IF EXISTS + CREATE TRIGGER）"
psql -v ON_ERROR_STOP=1 "$DATABASE_URL" -f /desired_extras.sql
echo "✅ Step 4 完成（触发器已重建）"

# ── Step 5: 应用种子数据 ──────────────────────────────────────────────────
# 必须晚于 Step 3（Atlas 建表）：种子里的 INSERT 直接依赖本次新建的表。
# 按文件名排序执行，与 db-seed Job / CI 的顺序保持一致。
echo ""
echo ">>> Step 5: 应用种子数据（/seeds/*.sql，按文件名顺序）"
if [ -d /seeds ]; then
  for SEED_FILE in $(ls /seeds/*.sql 2>/dev/null | sort); do
    echo "  执行: $SEED_FILE"
    psql -v ON_ERROR_STOP=1 "$DATABASE_URL" -f "$SEED_FILE"
  done
  echo "✅ Step 5 完成（种子数据已对齐）"
else
  # 旧镜像或自定义构建可能没有 /seeds：降到兼容模式，由 PostSync db-seed 兜底。
  echo "⏭️ 跳过：镜像内无 /seeds 目录"
fi

echo ""
echo "====== 迁移完成 ======"
