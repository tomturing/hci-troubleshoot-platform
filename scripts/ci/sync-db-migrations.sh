#!/bin/bash
# 同步迁移文件到 Helm ConfigMap
# 用法: scripts/ci/sync-db-migrations.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
MIGRATIONS_DIR="$PROJECT_ROOT/database/migrations"
CONFIGMAP_FILE="$PROJECT_ROOT/deploy/helm/hci-platform/templates/hooks/db-migrations-configmap.yaml"

echo "=== 同步数据库迁移到 ConfigMap ==="

# 检查迁移目录
if [ ! -d "$MIGRATIONS_DIR" ]; then
    echo "❌ 迁移目录不存在: $MIGRATIONS_DIR"
    exit 1
fi

# 获取所有迁移文件（按名称排序）
MIGRATION_FILES=$(ls -1 "$MIGRATIONS_DIR"/*.sql 2>/dev/null | sort)
if [ -z "$MIGRATION_FILES" ]; then
    echo "❌ 没有找到迁移文件"
    exit 1
fi

echo "找到迁移文件:"
echo "$MIGRATION_FILES" | xargs -I{} basename {}

# 备份原 ConfigMap
cp "$CONFIGMAP_FILE" "$CONFIGMAP_FILE.bak"

# 生成新的 data 部分
TEMP_FILE=$(mktemp)
echo "{{- if .Values.dbMigrate.enabled }}" > "$TEMP_FILE"
echo "# 此文件由 scripts/ci/sync-db-migrations.sh 自动生成" >> "$TEMP_FILE"
echo "# 请勿手动编辑，修改 database/migrations/ 后重新运行脚本" >> "$TEMP_FILE"
echo "apiVersion: v1" >> "$TEMP_FILE"
echo "kind: ConfigMap" >> "$TEMP_FILE"
echo "metadata:" >> "$TEMP_FILE"
echo "  name: db-migrations" >> "$TEMP_FILE"
echo "  namespace: {{ include \"hci.namespace\" . }}" >> "$TEMP_FILE"
echo "  labels:" >> "$TEMP_FILE"
echo "    {{- include \"hci.labels\" . | nindent 4 }}" >> "$TEMP_FILE"
echo "    app.kubernetes.io/component: db-migrate" >> "$TEMP_FILE"
echo "  annotations:" >> "$TEMP_FILE"
echo "    argocd.argoproj.io/hook: PreSync" >> "$TEMP_FILE"
echo "    argocd.argoproj.io/hook-delete-policy: BeforeHookCreation" >> "$TEMP_FILE"
echo "    helm.sh/hook-weight: \"-10\"" >> "$TEMP_FILE"
echo "data:" >> "$TEMP_FILE"

# 添加每个迁移文件
for migration_file in $MIGRATION_FILES; do
    filename=$(basename "$migration_file")
    echo "  $filename: |" >> "$TEMP_FILE"
    # 读取文件内容并缩进 4 空格
    sed 's/^/    /' "$migration_file" >> "$TEMP_FILE"
    echo "" >> "$TEMP_FILE"
done

echo "{{- end }}" >> "$TEMP_FILE"

# 替换原文件
mv "$TEMP_FILE" "$CONFIGMAP_FILE"

echo ""
echo "✅ ConfigMap 已更新: $CONFIGMAP_FILE"
echo "   备份文件: $CONFIGMAP_FILE.bak"