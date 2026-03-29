#!/usr/bin/env bash
# scripts/ops/staging-diff.sh
# 横向环境一致性工具：对比 dev 和 staging 环境的关键差异
#
# 对比维度：
#   1. 镜像 Tag 差异（Deployment image）
#   2. ConfigMap key 差异
#   3. DB Schema 差异（可选，需要 psql 访问权限）
#
# 用法：
#   bash scripts/ops/staging-diff.sh
#   bash scripts/ops/staging-diff.sh --skip-db        # 跳过 DB schema 对比
#   bash scripts/ops/staging-diff.sh --ns-dev hci-dev --ns-staging hci-staging

set -euo pipefail

# ──────────────────────────────────────────────────────────
# 颜色
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

info()    { echo -e "${CYAN}[INFO]${NC}  $*"; }
ok()      { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*"; }
section() { echo -e "\n${CYAN}════════════════════════════════════════${NC}"; echo -e "${CYAN}  $*${NC}"; echo -e "${CYAN}════════════════════════════════════════${NC}"; }

# ──────────────────────────────────────────────────────────
# 参数解析
NS_DEV="hci-dev"
NS_STAGING="hci-staging"
SKIP_DB=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --ns-dev)      NS_DEV="$2"; shift 2 ;;
    --ns-staging)  NS_STAGING="$2"; shift 2 ;;
    --skip-db)     SKIP_DB=true; shift ;;
    *) echo "未知参数: $1"; exit 1 ;;
  esac
done

KUBECTL="${KUBECTL:-kubectl}"

# ──────────────────────────────────────────────────────────
# 检查 namespace 是否存在
for NS in "$NS_DEV" "$NS_STAGING"; do
  if ! ${KUBECTL} get namespace "$NS" &>/dev/null; then
    warn "Namespace '$NS' 不存在，跳过对比（可能 staging 未部署）"
    exit 0
  fi
done

# ──────────────────────────────────────────────────────────
section "1. 镜像 Tag 对比（dev vs staging）"

DEV_IMAGES=$( ${KUBECTL} get deployments -n "$NS_DEV" --no-headers \
  -o custom-columns="NAME:.metadata.name,IMAGE:.spec.template.spec.containers[0].image" \
  2>/dev/null | sort )

STAGING_IMAGES=$( ${KUBECTL} get deployments -n "$NS_STAGING" --no-headers \
  -o custom-columns="NAME:.metadata.name,IMAGE:.spec.template.spec.containers[0].image" \
  2>/dev/null | sort )

IMAGE_DIFF=$(diff \
  <(echo "$DEV_IMAGES") \
  <(echo "$STAGING_IMAGES") \
  || true)

if [[ -z "$IMAGE_DIFF" ]]; then
  ok "镜像 Tag 一致"
else
  warn "镜像 Tag 存在差异（< dev，> staging）："
  echo "$IMAGE_DIFF" | grep "^[<>]" | while IFS= read -r line; do
    if [[ "$line" == "<"* ]]; then
      echo -e "  ${RED}${line}${NC}"
    else
      echo -e "  ${GREEN}${line}${NC}"
    fi
  done
fi

# ──────────────────────────────────────────────────────────
section "2. ConfigMap Key 对比"

for CM_NAME in hci-common-config; do
  DEV_KEYS=$(${KUBECTL} get configmap "$CM_NAME" -n "$NS_DEV" \
    -o jsonpath='{.data}' 2>/dev/null | python3 -c "
import json,sys; d=json.load(sys.stdin); print('\n'.join(sorted(d.keys())))
" 2>/dev/null || echo "(不存在)")

  STAGING_KEYS=$(${KUBECTL} get configmap "$CM_NAME" -n "$NS_STAGING" \
    -o jsonpath='{.data}' 2>/dev/null | python3 -c "
import json,sys; d=json.load(sys.stdin); print('\n'.join(sorted(d.keys())))
" 2>/dev/null || echo "(不存在)")

  CM_DIFF=$(diff <(echo "$DEV_KEYS") <(echo "$STAGING_KEYS") || true)
  if [[ -z "$CM_DIFF" ]]; then
    ok "ConfigMap '$CM_NAME' keys 一致"
  else
    warn "ConfigMap '$CM_NAME' keys 存在差异（< dev，> staging）："
    echo "$CM_DIFF"
  fi
done

# ──────────────────────────────────────────────────────────
section "3. DB Schema 对比（可选）"

if [[ "$SKIP_DB" == "true" ]]; then
  info "跳过 DB schema 对比（--skip-db）"
else
  DEV_POSTGRES="postgres-0"
  STAGING_POSTGRES="postgres-0"

  # 检查 postgres pod 是否就绪
  DEV_POSTGRES_READY=$(${KUBECTL} get pod "$DEV_POSTGRES" -n "$NS_DEV" \
    --no-headers 2>/dev/null | grep -c "Running" || echo "0")
  STAGING_POSTGRES_READY=$(${KUBECTL} get pod "$STAGING_POSTGRES" -n "$NS_STAGING" \
    --no-headers 2>/dev/null | grep -c "Running" || echo "0")

  if [[ "$DEV_POSTGRES_READY" -eq 0 || "$STAGING_POSTGRES_READY" -eq 0 ]]; then
    warn "PostgreSQL Pod 未就绪，跳过 DB schema 对比"
    warn "  dev postgres: $([ "$DEV_POSTGRES_READY" -eq 1 ] && echo 'Running' || echo '未就绪')"
    warn "  staging postgres: $([ "$STAGING_POSTGRES_READY" -eq 1 ] && echo 'Running' || echo '未就绪')"
  else
    DEV_SCHEMA=$(${KUBECTL} exec "$DEV_POSTGRES" -n "$NS_DEV" -- \
      pg_dump -s -U hci_admin hci_troubleshoot 2>/dev/null | grep -E "^CREATE TABLE|^ALTER TABLE|^CREATE INDEX" | sort || echo "")
    STAGING_SCHEMA=$(${KUBECTL} exec "$STAGING_POSTGRES" -n "$NS_STAGING" -- \
      pg_dump -s -U hci_admin hci_troubleshoot 2>/dev/null | grep -E "^CREATE TABLE|^ALTER TABLE|^CREATE INDEX" | sort || echo "")

    SCHEMA_DIFF=$(diff <(echo "$DEV_SCHEMA") <(echo "$STAGING_SCHEMA") || true)
    if [[ -z "$SCHEMA_DIFF" ]]; then
      ok "DB Schema（表/索引）一致"
    else
      warn "DB Schema 存在差异（< dev，> staging）："
      echo "$SCHEMA_DIFF"
      warn "请检查是否需要在 staging 执行迁移脚本"
    fi
  fi
fi

# ──────────────────────────────────────────────────────────
section "对比完成"
info "dev 命名空间: ${NS_DEV}"
info "staging 命名空间: ${NS_STAGING}"
info "如发现差异，可通过以下命令同步 staging："
info "  bash scripts/ops/k3s-deploy-dualrepo.sh --env staging"
