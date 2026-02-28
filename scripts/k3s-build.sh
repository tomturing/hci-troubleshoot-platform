#!/bin/bash
# =============================================================================
# HCI 平台 — Docker 镜像构建 & 导入 K3s
# =============================================================================
# 使用方法: bash scripts/k3s-build.sh [--no-import]
#   --no-import  仅构建不导入 K3s（用于 CI 环境仅构建镜像）
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

info()  { echo -e "${BLUE}[INFO]${NC}  $*"; }
ok()    { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; }

IMPORT_K3S=true
[[ "${1:-}" == "--no-import" ]] && IMPORT_K3S=false

# ============================================================================
# 镜像列表: name  context  dockerfile
# ============================================================================
declare -a IMAGES=(
  "hci-api-gateway            ${PROJECT_ROOT}/backend       api-gateway/Dockerfile"
  "hci-case-service           ${PROJECT_ROOT}/backend       case-service/Dockerfile"
  "hci-conversation-service   ${PROJECT_ROOT}/backend       conversation-service/Dockerfile"
  "hci-scheduler-service      ${PROJECT_ROOT}/backend       scheduler-service/Dockerfile"
  "hci-customer-ui            ${PROJECT_ROOT}/frontend      customer/Dockerfile"
  "hci-admin-ui               ${PROJECT_ROOT}/frontend      admin/Dockerfile"
)

# ============================================================================
# 构建所有镜像
# ============================================================================
info "开始构建 ${#IMAGES[@]} 个 Docker 镜像..."
echo ""

BUILT=0
FAILED=0

for entry in "${IMAGES[@]}"; do
  read -r name context dockerfile <<< "$entry"
  info "[$((BUILT+FAILED+1))/${#IMAGES[@]}] 构建 ${name}:latest ..."
  
  if docker build -t "${name}:latest" -f "${context}/${dockerfile}" "${context}" --quiet; then
    ok "  → ${name}:latest 构建成功"
    ((BUILT++))
  else
    error "  → ${name}:latest 构建失败!"
    ((FAILED++))
  fi
done

echo ""
info "构建完成: ${GREEN}${BUILT} 成功${NC}, ${RED}${FAILED} 失败${NC}"

if [[ $FAILED -gt 0 ]]; then
  error "存在构建失败，请修复后重试"
  exit 1
fi

# ============================================================================
# 导入 K3s containerd
# ============================================================================
if [[ "$IMPORT_K3S" == true ]]; then
  echo ""
  info "开始导入镜像到 K3s containerd..."
  
  # 检查 k3s 是否可用
  if ! command -v k3s &>/dev/null && ! sudo k3s --version &>/dev/null 2>&1; then
    error "k3s 未安装或无法访问"
    exit 1
  fi

  IMPORTED=0
  for entry in "${IMAGES[@]}"; do
    read -r name _ _ <<< "$entry"
    info "  导入 ${name}:latest ..."
    if docker save "${name}:latest" | sudo k3s ctr images import -; then
      ok "  → ${name}:latest 已导入"
      ((IMPORTED++))
    else
      error "  → ${name}:latest 导入失败"
    fi
  done
  
  echo ""
  info "导入完成: ${IMPORTED}/${#IMAGES[@]}"
  
  echo ""
  info "验证 K3s 中的 HCI 镜像:"
  sudo k3s ctr images ls | grep "hci-" | awk '{print "  " $1}' || true
fi

echo ""
ok "全部完成! ✅"
