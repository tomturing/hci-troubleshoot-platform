#!/bin/bash
# 检查数据库迁移是否已同步到 Helm ConfigMap，并检测重复 version 号
# 用法: scripts/ci/check-db-migrations-sync.sh
# 返回值: 0=已同步且无重复, 1=未同步或有重复 version

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
MIGRATIONS_DIR="$PROJECT_ROOT/database/migrations"
CONFIGMAP_FILE="$PROJECT_ROOT/deploy/helm/hci-platform/templates/hooks/db-migrations-configmap.yaml"

echo "=== 检查数据库迁移同步状态 ==="

# 检查迁移目录
if [ ! -d "$MIGRATIONS_DIR" ]; then
    echo "❌ 迁移目录不存在: $MIGRATIONS_DIR"
    exit 1
fi

# 获取所有迁移文件
MIGRATION_FILES=$(ls -1 "$MIGRATIONS_DIR"/*.sql 2>/dev/null | sort)
if [ -z "$MIGRATION_FILES" ]; then
    echo "⚠️  没有找到迁移文件"
    exit 0
fi

# ─── 检查重复 version 号 ─────────────────────────────────────────────────────
# dbmate 以文件名开头纯数字为主键，重复 version 会导致第二个文件被静默跳过
echo ""
echo "检查 version 号唯一性:"
echo "----------------------------------------"
DUPLICATE_VERSIONS=$(echo "$MIGRATION_FILES" \
    | xargs -I{} basename {} \
    | grep -oP '^\d+' \
    | sort \
    | uniq -d)

if [ -n "$DUPLICATE_VERSIONS" ]; then
    echo "❌ 检测到重复的 version 号（dbmate 会静默跳过重复项）："
    for v in $DUPLICATE_VERSIONS; do
        echo "   version=$v，对应文件："
        echo "$MIGRATION_FILES" | xargs -I{} basename {} | grep "^${v}_" | sed 's/^/     - /'
    done
    echo ""
    echo "修复方法：将重复文件重命名为下一个可用序号（如 YYYYMMDD002_xxx.sql）"
    DUPLICATE_ERROR=1
else
    echo "✅ 所有文件 version 号唯一"
    DUPLICATE_ERROR=0
fi

MISSING_COUNT=0
PRESENT_COUNT=0

echo ""
echo "检查迁移文件同步状态:"
echo "----------------------------------------"

for migration_file in $MIGRATION_FILES; do
    filename=$(basename "$migration_file")

    # 检查 ConfigMap 中是否包含该迁移文件
    if grep -q "^  ${filename}:" "$CONFIGMAP_FILE" 2>/dev/null; then
        echo "✅ $filename"
        PRESENT_COUNT=$((PRESENT_COUNT + 1))
    else
        echo "❌ $filename (缺失)"
        MISSING_COUNT=$((MISSING_COUNT + 1))
    fi
done

echo "----------------------------------------"
echo "已同步: $PRESENT_COUNT"
echo "未同步: $MISSING_COUNT"
echo ""

if [ $MISSING_COUNT -gt 0 ]; then
    echo "❌ 检测到 $MISSING_COUNT 个迁移文件未同步到 ConfigMap"
    echo ""
    echo "请运行以下命令同步:"
    echo "  scripts/ci/sync-db-migrations.sh"
    echo ""
    echo "或手动添加迁移文件到:"
    echo "  deploy/helm/hci-platform/templates/hooks/db-migrations-configmap.yaml"
    exit 1
elif [ "$DUPLICATE_ERROR" -eq 1 ]; then
    echo "❌ 存在重复 version 号，请修复后重新提交"
    exit 1
else
    echo "✅ 所有迁移文件已同步到 ConfigMap，version 号唯一"
    exit 0
fi