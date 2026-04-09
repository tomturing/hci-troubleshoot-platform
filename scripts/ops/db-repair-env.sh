#!/usr/bin/env bash
# =============================================================================
# DB Schema 修复工具 — 在指定 K8s namespace 中执行历史 schema_repair SQL
# 用法: ./scripts/ops/db-repair-env.sh <namespace>
# 示例: ./scripts/ops/db-repair-env.sh hci-staging
#
# 前提：
#   - kubectl 已配置且可访问目标集群
#   - 目标 namespace 中有 postgres-0 StatefulSet Pod
#   - docs/archive/db-migrations-history/migrations/20260407001_schema_repair.sql 存在
#
# 注意：此脚本执行历史修复 SQL（已归档），不经过 Atlas 迁移链。
#   常规 schema 变更请使用 Atlas（atlas migrate diff / apply）。
#
# 安全性：
#   - 所有 DDL 使用 IF NOT EXISTS，可重复执行
#   - 不删除任何数据，仅添加表/列
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
# 历史 dbmate 迁移文件已归档至 docs/archive/db-migrations-history/
MIGRATION_FILE="${REPO_ROOT}/docs/archive/db-migrations-history/migrations/20260407001_schema_repair.sql"

NS="${1:?用法: $0 <namespace>  示例: hci-dev / hci-staging / hci-prod}"
DB_USER="${2:-hci_admin}"
DB_NAME="${3:-hci_troubleshoot}"
# v6.x 目标表数（17 张业务表）
EXPECTED_TABLE_COUNT=17

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }

# 检查前提
if [[ ! -f "${MIGRATION_FILE}" ]]; then
    error "修复 SQL 文件不存在: ${MIGRATION_FILE}"
    exit 1
fi

if ! kubectl get pod postgres-0 -n "${NS}" &>/dev/null; then
    error "找不到 ${NS}/postgres-0，请确认 namespace 和 Pod 名称"
    exit 1
fi

echo "============================================"
info "目标环境: ${NS}"
info "数据库: ${DB_NAME} (用户: ${DB_USER})"
echo "============================================"

# 步骤 1：诊断
info ">>> 步骤 1/5：诊断当前状态"
echo "--- 当前表列表 ---"
kubectl exec postgres-0 -n "${NS}" -- psql -U "${DB_USER}" -d "${DB_NAME}" -c "
SELECT table_name FROM information_schema.tables
WHERE table_schema='public' AND table_type='BASE TABLE'
ORDER BY table_name;" 2>/dev/null

echo "--- 当前 Atlas 迁移记录 ---"
kubectl exec postgres-0 -n "${NS}" -- psql -U "${DB_USER}" -d "${DB_NAME}" -c "
SELECT version, description, applied_at FROM atlas_schema_revisions ORDER BY version;" 2>/dev/null \
    || warn "atlas_schema_revisions 表不存在（Atlas 尚未执行过迁移？）"

# 步骤 2：备份提醒
if [[ "${NS}" == *"prod"* ]]; then
    warn ">>> 生产环境！建议先执行 pg_dump 备份："
    warn "    kubectl exec postgres-0 -n ${NS} -- pg_dump -U ${DB_USER} ${DB_NAME} > /tmp/backup_$(date +%Y%m%d).sql"
    read -r -p "已完成备份？继续执行？(y/N) " confirm
    if [[ "${confirm}" != "y" && "${confirm}" != "Y" ]]; then
        info "已取消"
        exit 0
    fi
fi

# 步骤 3：执行修复
info ">>> 步骤 3/5：复制并执行修复 SQL"
kubectl cp "${MIGRATION_FILE}" "${NS}/postgres-0:/tmp/schema_repair.sql" 2>/dev/null
kubectl exec postgres-0 -n "${NS}" -- psql -U "${DB_USER}" -d "${DB_NAME}" \
    -v ON_ERROR_STOP=1 -f /tmp/schema_repair.sql 2>&1

# 步骤 4：说明（此修复 SQL 在 Atlas 迁移链之外，无需手动注册版本）
info ">>> 步骤 4/5：Atlas 迁移状态说明"
warn "此修复 SQL 属于历史归档文件，不在 Atlas 迁移链中，无需注册版本。"
warn "后续 schema 变更请通过 atlas migrate diff + atlas migrate apply 完成。"
info "当前 Atlas 迁移状态："
kubectl exec postgres-0 -n "${NS}" -- psql -U "${DB_USER}" -d "${DB_NAME}" -c "
SELECT version, description, applied_at FROM atlas_schema_revisions ORDER BY version;" 2>/dev/null \
    || warn "atlas_schema_revisions 表不存在，可在下次 Helm Sync 时由 Atlas 自动创建"

# 步骤 5：验证
info ">>> 步骤 5/5：验证结果"
TABLE_COUNT=$(kubectl exec postgres-0 -n "${NS}" -- psql -U "${DB_USER}" -d "${DB_NAME}" -t -c "
SELECT COUNT(*) FROM information_schema.tables
WHERE table_schema='public' AND table_type='BASE TABLE';" 2>/dev/null | tr -d '[:space:]')

echo "============================================"
if [[ "${TABLE_COUNT}" -ge "${EXPECTED_TABLE_COUNT}" ]]; then
    info "✅ 表数量: ${TABLE_COUNT} (≥${EXPECTED_TABLE_COUNT}，v6.x 目标：${EXPECTED_TABLE_COUNT} 张业务表)"
else
    error "❌ 表数量: ${TABLE_COUNT} (<${EXPECTED_TABLE_COUNT}，可能有建表缺失)"
fi

echo "--- 最终 Atlas 迁移记录 ---"
kubectl exec postgres-0 -n "${NS}" -- psql -U "${DB_USER}" -d "${DB_NAME}" -c "
SELECT version, description, applied_at FROM atlas_schema_revisions ORDER BY version;" 2>/dev/null

echo "============================================"
info "修复完成！"
info "下一步："
info "  1. 验证服务健康：kubectl logs -n ${NS} deploy/case-service --tail=5"
info "  2. 如需启用自动迁移（Atlas），修改对应环境的 values.yaml："
info "     dbMigrate:"
info "       enabled: true"
echo "============================================"
