#!/bin/sh
# ============================================================================
# Migration Runner — 版本化数据迁移执行器
# ============================================================================
# 功能：
#   1. 确保 migration_history 表存在
#   2. 按版本号顺序执行 data-migrations/ 目录下的 SQL 文件
#   3. 记录执行历史（version, checksum, executed_at）
#   4. 幂等性保证：已执行的迁移自动跳过
#
# 使用方式：
#   ./migration-runner.sh
#
# 环境变量（由 db-migrate.sh 注入）：
#   DATABASE_URL - 目标数据库连接串
# ============================================================================

set -e

MIGRATIONS_DIR="/data-migrations"

echo "====== Migration Runner ======"
echo "Migrations directory: ${MIGRATIONS_DIR}"
echo ""

# ── Step 0: 确保 migration_history 表存在 ───────────────────────────────────
echo ">>> Step 0: 确保 migration_history 表存在"
psql "$DATABASE_URL" -c "
CREATE TABLE IF NOT EXISTS migration_history (
    version VARCHAR(100) PRIMARY KEY,
    checksum VARCHAR(64) NOT NULL,
    description VARCHAR(255),
    executed_at TIMESTAMPTZ DEFAULT NOW(),
    execution_time_ms INTEGER
);
" > /dev/null 2>&1
echo "✅ migration_history 表已就绪"
echo ""

# ── Step 1: 检查迁移目录是否存在 ────────────────────────────────────────────
if [ ! -d "$MIGRATIONS_DIR" ]; then
    echo "⚠️  迁移目录不存在: ${MIGRATIONS_DIR}"
    echo "跳过数据迁移"
    exit 0
fi

# 统计迁移文件数量
MIGRATION_COUNT=$(ls -1 "${MIGRATIONS_DIR}"/*.sql 2>/dev/null | wc -l)
if [ "$MIGRATION_COUNT" -eq 0 ]; then
    echo "⚠️  没有找到迁移文件"
    echo "跳过数据迁移"
    exit 0
fi

echo "发现 ${MIGRATION_COUNT} 个迁移文件"
echo ""

# ── Step 2: 按版本号顺序执行迁移 ────────────────────────────────────────────
EXECUTED=0
SKIPPED=0
FAILED=0

for file in $(ls -1 "${MIGRATIONS_DIR}"/*.sql 2>/dev/null | sort); do
    filename=$(basename "$file" .sql)
    
    # 解析版本号和描述（格式：{version}_{description}.sql）
    version=$(echo "$filename" | cut -d_ -f1)
    description=$(echo "$filename" | cut -d_ -f2- | tr '_' ' ')
    
    # 计算文件校验和
    checksum=$(sha256sum "$file" | cut -d' ' -f1)
    
    # 检查是否已执行
    exists=$(psql "$DATABASE_URL" -t -c \
        "SELECT 1 FROM migration_history WHERE version = '$version';" 2>/dev/null | tr -d '[:space:]')
    
    if [ "$exists" = "1" ]; then
        echo "⏭️  Migration ${version} already executed, skipping"
        SKIPPED=$((SKIPPED + 1))
        continue
    fi
    
    echo "▶️  Executing migration: ${version} (${description})"
    
    # 记录开始时间
    start_time=$(date +%s%3N 2>/dev/null || date +%s000)
    
    # 执行迁移
    if psql -v ON_ERROR_STOP=1 "$DATABASE_URL" -f "$file"; then
        # 记录结束时间
        end_time=$(date +%s%3N 2>/dev/null || date +%s000)
        execution_time=$((end_time - start_time))
        
        # 记录执行历史
        psql "$DATABASE_URL" -c \
            "INSERT INTO migration_history (version, checksum, description, execution_time_ms)
             VALUES ('$version', '$checksum', '$description', $execution_time);" > /dev/null 2>&1
        
        echo "✅ Migration ${version} completed in ${execution_time}ms"
        EXECUTED=$((EXECUTED + 1))
    else
        echo "❌ Migration ${version} failed"
        FAILED=$((FAILED + 1))
        # 继续执行下一个迁移（可根据需求改为 exit 1）
    fi
    
    echo ""
done

# ── Step 3: 输出统计 ──────────────────────────────────────────────────────────
echo "====== Migration Summary ======"
echo "Executed: ${EXECUTED}"
echo "Skipped:  ${SKIPPED}"
echo "Failed:   ${FAILED}"
echo ""

if [ "$FAILED" -gt 0 ]; then
    echo "❌ 有迁移执行失败，请检查日志"
    exit 1
fi

echo "✅ 所有数据迁移已完成"