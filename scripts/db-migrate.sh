#!/bin/sh
# db-migrate 容器入口脚本
# 执行顺序（严格串行）：
#   0. 空库先用 Atlas 创建基础表；存量库跳过，避免约束先于数据清洗
#   1. 预迁移：migration-runner 第一趟（允许依赖新表的迁移延后重试）
#   2. psql 应用函数（desired_extras.sql，幂等：CREATE OR REPLACE FUNCTION）
#   3. atlas schema apply（最终声明式差量收敛：建新表 + 施加约束）
#   3.5 最终迁移：migration-runner 第二趟（必须全部成功）
#   4. psql 应用触发器（需要表已存在，幂等：DROP TRIGGER IF EXISTS + CREATE TRIGGER）
#   5. psql 按文件名顺序执行种子数据（seeds/，幂等：ON CONFLICT DO NOTHING）
#
# 为什么数据迁移必须跑两趟（2026-11-13 dev PreSync 死锁根治）：
#   - 存量库 Step 0 跳过建表，数据迁移却在 Step 3 建表之前执行；056~064 依赖
#     log_collect_* 新表，第一趟必然失败。旧脚本 set -e 立即终止，Step 3 永不
#     执行 —— 缺表导致迁移失败、迁移失败又阻止建表，构成闭环死锁。
#   - 顺序也不能简单对调：041 负责清洗 kb_category.code 脏数据，必须先于
#     Step 3 由 Atlas 施加 kb_category_code_format CHECK，否则 Schema 收敛失败。
#   - 因此把两类依赖拆开：第一趟完成所有「约束前置」的数据清洗，Step 3 收敛
#     Schema（新表已存在、脏数据已清洗），第二趟补齐所有「依赖新表」的迁移。
#     migration-runner 按 version 判重：成功的不重跑，失败的不写 migration_history，
#     第二趟自然重试。任何真有缺陷的迁移会在第二趟再次失败并让 Job 失败，
#     不会被静默吞掉。
#
# 为什么 Step 5 不能提前、也不能只靠 Helm 的 PostSync db-seed：
#   - 种子描述的是「本次 Schema 收敛后的初始/参考数据」，必须晚于 Step 3；
#   - PostSync db-seed 仍然保留（Helm 直装与兼容路径），两边重复执行一次，
#     所有种子均幂等（02/03/06 DO NOTHING 保护运营改动，01 DO UPDATE 是代码强约束）。
#
# 前置条件：
#   - 数据库用户拥有安装 vector/pgcrypto/uuid-ossp/pg_trgm extensions 的权限
#   - atlas_dev 数据库已就绪
#
# 环境变量（由 Helm Job 注入；资源路径可由 CI 覆盖以复现真实执行序）：
#   DATABASE_URL            - 目标数据库连接串
#   DEV_URL                 - Atlas dev 数据库连接串（空数据库，用于 SQL 规范化）
#   HCI_MIGRATION_TRACE_ID  - 唯一调用链标识，缺省自动生成，串联本次全部日志
#   SCHEMA_FILE / EXTRAS_FILE / MIGRATION_RUNNER / MIGRATIONS_DIR / SEEDS_DIR
#                           - 镜像内资源路径，缺省为容器绝对路径

set -e

# 唯一调用链：一次 PreSync 迁移的所有日志共享同一 trace_id，可据此还原
# 「扩展 → 预迁移 → 函数 → Atlas → 最终迁移 → 触发器 → 种子」完整调用路径。
HCI_MIGRATION_TRACE_ID="${HCI_MIGRATION_TRACE_ID:-dbmigrate-$(date +%Y%m%d%H%M%S)-$$}"
export HCI_MIGRATION_TRACE_ID

SCHEMA_FILE="${SCHEMA_FILE:-/desired_schema.sql}"
EXTRAS_FILE="${EXTRAS_FILE:-/desired_extras.sql}"
MIGRATION_RUNNER="${MIGRATION_RUNNER:-/migration-runner.sh}"
MIGRATIONS_DIR="${MIGRATIONS_DIR:-/data-migrations}"
SEEDS_DIR="${SEEDS_DIR:-/seeds}"
export MIGRATIONS_DIR

log() {
  printf '[trace_id=%s] %s\n' "$HCI_MIGRATION_TRACE_ID" "$*"
}

log "====== HCI DB 声明式迁移 ======"
log "目标数据库: 已配置（连接串已脱敏）"

# 目标库的类型与函数扩展是 desired_schema.sql 的硬依赖。迁移入口自行确保，
# 避免 Docker Compose、Helm 和 CI 对扩展初始化顺序产生隐式差异。
log ">>> 确保目标数据库扩展对象"
psql -v ON_ERROR_STOP=1 "$DATABASE_URL" <<'SQL'
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS pg_trgm;
SQL

# Atlas 在规范化文件 Schema 时会清理 dev 数据库的 public schema。pgvector 等扩展
# 记录可能仍存在，但 vector 类型/函数已随 schema 被删除；下一次 apply 前必须重建。
prepare_atlas_dev() {
  log ">>> 重置 Atlas dev 数据库扩展对象"
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

apply_schema() {
  atlas schema apply \
    --url "$DATABASE_URL" \
    --to "file://$SCHEMA_FILE" \
    --dev-url "$DEV_URL" \
    --exclude "schema_migrations,alembic_version,atlas_schema_revisions" \
    --auto-approve
}

# ── Step 0: 空库 Schema 引导 ────────────────────────────────────────────────
# 数据迁移包含对 tool_definition/system_prompt/kbd_entry 等业务表的 DML。
# 存量库必须先迁数据再收敛新约束；全新空库则必须先创建这些基础表。
CORE_SCHEMA_READY=$(psql -v ON_ERROR_STOP=1 "$DATABASE_URL" -tAc \
  "SELECT CASE WHEN to_regclass('public.tool_definition') IS NULL THEN 'false' ELSE 'true' END;")

if [ "$CORE_SCHEMA_READY" != "true" ]; then
  log ""
  log ">>> Step 0: 检测到全新或未完成初始化的数据库，执行 Atlas 基础 Schema 引导"
  prepare_atlas_dev
  apply_schema
  log "✅ Step 0 完成（数据迁移依赖表已就绪）"
else
  log ""
  log ">>> Step 0: 核心 Schema 已存在，进入存量库两趟迁移升级路径"
fi

# ── Step 1: 预迁移（第一趟）────────────────────────────────────────────────
# 这一趟必须完成所有「Schema 约束前置」的数据清洗（如 041 清洗分类码）。
# 依赖 Step 3 才创建的新表的迁移允许失败，但失败不会被登记，由第二趟重试。
# 局部关闭 set -e 是刻意的：此处失败表示「前置条件尚未满足」，不是「迁移有缺陷」；
# 是否真有缺陷由第二趟（set -e 生效）判定，避免静默吞错。
log ""
log ">>> Step 1: 预迁移（migration-runner 第一趟）"
set +e
sh "$MIGRATION_RUNNER"
PRE_MIGRATE_RC=$?
set -e
if [ "$PRE_MIGRATE_RC" -ne 0 ]; then
  log "⚠️ Step 1 预迁移存在未完成版本（rc=${PRE_MIGRATE_RC}），将在 Step 3 Schema 收敛后重试"
else
  log "✅ Step 1 预迁移完成，无需重试"
fi

# ── Step 2: 应用函数 ────────────────────────────────────────────────────────
log ""
log ">>> Step 2: 应用函数（CREATE OR REPLACE FUNCTION）"
psql -v ON_ERROR_STOP=1 "$DATABASE_URL" -f "$EXTRAS_FILE"
log "✅ Step 2 完成"

# ── Step 3: Atlas 声明式 Schema 最终收敛 ────────────────────────────────────
log ""
log ">>> Step 3: Atlas schema apply（最终声明式差量收敛）"
prepare_atlas_dev
apply_schema
log "✅ Step 3 完成"

# ── Step 3.5: 最终迁移（第二趟）───────────────────────────────────────────
# 此刻数据迁移所依赖的新表已由 Step 3 创建、脏数据已由第一趟清洗。
# 这里 set -e 生效：任何版本再次失败都会让 PreSync Job 失败，绝不降级通过。
log ""
log ">>> Step 3.5: 最终数据迁移（migration-runner 第二趟，必须全部成功）"
sh "$MIGRATION_RUNNER"
log "✅ Step 3.5 完成（所有版本化迁移均已登记）"

# ── Step 4: 应用触发器 ────────────────────────────────────────────────────
# 触发器依赖表结构，必须在 Atlas 创建/更新表后执行
log ""
log ">>> Step 4: 重建触发器（DROP TRIGGER IF EXISTS + CREATE TRIGGER）"
psql -v ON_ERROR_STOP=1 "$DATABASE_URL" -f "$EXTRAS_FILE"
log "✅ Step 4 完成（触发器已重建）"

# ── Step 5: 应用种子数据 ──────────────────────────────────────────────────
# 必须晚于 Step 3（Atlas 建表）：种子里的 INSERT 直接依赖本次新建的表。
# 按文件名排序执行，与 db-seed Job / CI 的顺序保持一致。
log ""
log ">>> Step 5: 应用种子数据（按文件名顺序）"
if [ -d "$SEEDS_DIR" ]; then
  for SEED_FILE in $(ls "$SEEDS_DIR"/*.sql 2>/dev/null | sort); do
    log "  执行: $SEED_FILE"
    psql -v ON_ERROR_STOP=1 "$DATABASE_URL" -f "$SEED_FILE"
  done
  log "✅ Step 5 完成（种子数据已对齐）"
else
  # 旧镜像或自定义构建可能没有 seeds 目录：降到兼容模式，由 PostSync db-seed 兜底。
  log "⏭️ 跳过：镜像内无 seeds 目录（${SEEDS_DIR}）"
fi

log ""
log "====== 迁移完成 ======"
