#!/bin/bash
# 检查数据库迁移是否已同步到 Helm ConfigMap
# 用法: scripts/ci/check-db-migrations-sync.sh
# 返回值: 0=已同步, 1=未同步

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
else
    echo "✅ 所有迁移文件已同步到 ConfigMap"
    exit 0
fi