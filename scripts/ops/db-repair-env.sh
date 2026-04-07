#!/usr/bin/env bash
# =============================================================================
# DB Schema 修复工具 — 在指定 K8s namespace 中执行 schema_repair 迁移
# 用法: ./scripts/ops/db-repair-env.sh <namespace>
# 示例: ./scripts/ops/db-repair-env.sh hci-staging
#
# 前提：
#   - kubectl 已配置且可访问目标集群
#   - 目标 namespace 中有 postgres-0 StatefulSet Pod
#   - database/20260407001_schema_repair.sql 存在
#
# 安全性：
#   - 所有 DDL 使用 IF NOT EXISTS，可重复执行
#   - 不删除任何数据，仅添加表/列
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
MIGRATION_FILE="${REPO_ROOT}/database/20260407001_schema_repair.sql"

NS="${1:?用法: $0 <namespace>  示例: hci-dev / hci-staging / hci-prod}"
DB_USER="${2:-hci_admin}"
DB_NAME="${3:-hci_troubleshoot}"

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

echo "--- 当前迁移记录 ---"
kubectl exec postgres-0 -n "${NS}" -- psql -U "${DB_USER}" -d "${DB_NAME}" -c "
SELECT version FROM schema_migrations ORDER BY version;" 2>/dev/null || warn "schema_migrations 表不存在（首次部署？）"

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
    -f /tmp/schema_repair.sql 2>&1

# 步骤 4：注册迁移版本
info ">>> 步骤 4/5：注册迁移版本到 schema_migrations"
kubectl exec postgres-0 -n "${NS}" -- psql -U "${DB_USER}" -d "${DB_NAME}" -c "
INSERT INTO schema_migrations (version) VALUES
    ('20260305001'),('20260312001'),('20260312002'),
    ('20260326001'),('20260326002'),('20260326003'),
    ('20260401001'),('20260407001')
ON CONFLICT DO NOTHING;" 2>/dev/null

# 步骤 5：验证
info ">>> 步骤 5/5：验证结果"
TABLE_COUNT=$(kubectl exec postgres-0 -n "${NS}" -- psql -U "${DB_USER}" -d "${DB_NAME}" -t -c "
SELECT COUNT(*) FROM information_schema.tables
WHERE table_schema='public' AND table_type='BASE TABLE';" 2>/dev/null | tr -d ' ')

MIGRATION_COUNT=$(kubectl exec postgres-0 -n "${NS}" -- psql -U "${DB_USER}" -d "${DB_NAME}" -t -c "
SELECT COUNT(*) FROM schema_migrations;" 2>/dev/null | tr -d ' ')

echo "============================================"
if [[ "${TABLE_COUNT}" -ge 20 ]]; then
    info "✅ 表数量: ${TABLE_COUNT} (≥20)"
else
    error "❌ 表数量: ${TABLE_COUNT} (<20，可能有问题)"
fi
info "迁移版本数: ${MIGRATION_COUNT}"

echo "--- 最终迁移记录 ---"
kubectl exec postgres-0 -n "${NS}" -- psql -U "${DB_USER}" -d "${DB_NAME}" -c "
SELECT version FROM schema_migrations ORDER BY version;" 2>/dev/null

echo "============================================"
info "修复完成！"
info "下一步："
info "  1. 验证服务健康：kubectl logs -n ${NS} deploy/case-service --tail=5"
info "  2. 如需启用自动迁移，修改 hci-platform-env environments/${NS#hci-}/values.yaml:"
info "     dbMigrate:"
info "       enabled: true"
echo "============================================"
