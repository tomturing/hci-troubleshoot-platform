#!/bin/bash
# =============================================================================
# HCI 平台 — Helm 部署管理脚本
# =============================================================================
# 使用方法:
#   bash scripts/k3s-deploy.sh install    # 首次安装
#   bash scripts/k3s-deploy.sh upgrade    # 升级
#   bash scripts/k3s-deploy.sh uninstall  # 卸载
#   bash scripts/k3s-deploy.sh status     # 查看状态
#   bash scripts/k3s-deploy.sh lint       # Helm lint 检查
#   bash scripts/k3s-deploy.sh template   # 渲染模板到 stdout
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

# Helm 配置
RELEASE_NAME="hci-platform"
CHART_PATH="${PROJECT_ROOT}/deploy/helm/hci-platform"
NAMESPACE="hci-troubleshoot"
OBS_NAMESPACE="hci-observability"
VALUES_FILE="${CHART_PATH}/values.yaml"
VALUES_DEV_FILE="${CHART_PATH}/values-dev.yaml"
# 本地覆盖文件（含实际密钥，不入 git），放在 .local/values-prod.override.yaml
VALUES_OVERRIDE_FILE="${PROJECT_ROOT}/.local/values-prod.override.yaml"
KUBECTL="sudo k3s kubectl"

# 颜色
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

info()  { echo -e "${BLUE}[INFO]${NC}  $*"; }
ok()    { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; }

# ============================================================================
# 子命令实现
# ============================================================================

cmd_lint() {
  info "运行 Helm lint..."
  helm lint "${CHART_PATH}" -f "${VALUES_FILE}" -f "${VALUES_DEV_FILE}"
  ok "Lint 通过! ✅"
}

cmd_template() {
  info "渲染 Helm 模板..."
  helm template "${RELEASE_NAME}" "${CHART_PATH}" \
    -f "${VALUES_FILE}" \
    -f "${VALUES_DEV_FILE}"
}

cmd_install() {
  info "首次安装 HCI 平台到 K3s..."
  
  # 创建主 namespace（如果不存在）
  ${KUBECTL} create namespace "${NAMESPACE}" --dry-run=client -o yaml | ${KUBECTL} apply -f -
  
  # 停止 Docker Compose 服务（避免端口冲突）
  if docker compose -f "${PROJECT_ROOT}/deploy/docker/docker-compose.yml" ps --status running --quiet 2>/dev/null | head -1 | grep -q .; then
    warn "检测到 Docker Compose 服务正在运行，正在停止..."
    docker compose -f "${PROJECT_ROOT}/deploy/docker/docker-compose.yml" down 2>/dev/null || true
  fi
  
  local WSL_IP=$(hostname -I | awk '{print $1}')
  
  # 构建 values 参数（如果存在 override 文件则额外加载）
  local VALUES_ARGS=("-f" "${VALUES_FILE}" "-f" "${VALUES_DEV_FILE}")
  if [[ -f "${VALUES_OVERRIDE_FILE}" ]]; then
    VALUES_ARGS+=("-f" "${VALUES_OVERRIDE_FILE}")
    info "加载本地覆盖配置: ${VALUES_OVERRIDE_FILE}"
  fi

  # Helm install
  helm install "${RELEASE_NAME}" "${CHART_PATH}" \
    "${VALUES_ARGS[@]}" \
    --set "global.domain=${WSL_IP}.nip.io" \
    --namespace "${NAMESPACE}" \
    --create-namespace \
    --wait \
    --timeout 5m
  
  ok "Helm install 完成!"
  echo ""
  
  # 等待 Pod 就绪
  wait_for_pods
}

cmd_upgrade() {
  info "升级 HCI 平台..."
  
  local WSL_IP=$(hostname -I | awk '{print $1}')

  # 构建 values 参数（如果存在 override 文件则额外加载）
  local VALUES_ARGS=("-f" "${VALUES_FILE}" "-f" "${VALUES_DEV_FILE}")
  if [[ -f "${VALUES_OVERRIDE_FILE}" ]]; then
    VALUES_ARGS+=("-f" "${VALUES_OVERRIDE_FILE}")
    info "加载本地覆盖配置: ${VALUES_OVERRIDE_FILE}"
  fi
  
  helm upgrade "${RELEASE_NAME}" "${CHART_PATH}" \
    "${VALUES_ARGS[@]}" \
    --set "global.domain=${WSL_IP}.nip.io" \
    --namespace "${NAMESPACE}" \
    --wait \
    --timeout 5m
  
  ok "Helm upgrade 完成!"
  echo ""
  wait_for_pods
}

cmd_uninstall() {
  warn "正在卸载 HCI 平台..."
  
  helm uninstall "${RELEASE_NAME}" --namespace "${NAMESPACE}" 2>/dev/null || true
  
  # 清理 PVC（可选）
  read -p "是否删除持久化数据 (PVC)? [y/N] " -n 1 -r
  echo
  if [[ $REPLY =~ ^[Yy]$ ]]; then
    ${KUBECTL} delete pvc --all -n "${NAMESPACE}" 2>/dev/null || true
    ${KUBECTL} delete pvc --all -n "${OBS_NAMESPACE}" 2>/dev/null || true
    warn "PVC 已清理"
  fi
  
  ok "卸载完成"
}

cmd_status() {
  info "=== Helm Release 状态 ==="
  helm status "${RELEASE_NAME}" --namespace "${NAMESPACE}" 2>/dev/null || warn "Release 未安装"
  
  echo ""
  info "=== 业务 Pod 状态 (${NAMESPACE}) ==="
  ${KUBECTL} get pods -n "${NAMESPACE}" -o wide 2>/dev/null || true
  
  echo ""
  info "=== 可观测性 Pod 状态 (${OBS_NAMESPACE}) ==="
  ${KUBECTL} get pods -n "${OBS_NAMESPACE}" -o wide 2>/dev/null || true
  
  echo ""
  info "=== Services ==="
  ${KUBECTL} get svc -n "${NAMESPACE}" 2>/dev/null || true
  ${KUBECTL} get svc -n "${OBS_NAMESPACE}" 2>/dev/null || true
  
  echo ""
  info "=== Ingress ==="
  ${KUBECTL} get ingress -n "${NAMESPACE}" 2>/dev/null || true
  
  echo ""
  info "=== PVC ==="
  ${KUBECTL} get pvc -n "${NAMESPACE}" 2>/dev/null || true
  ${KUBECTL} get pvc -n "${OBS_NAMESPACE}" 2>/dev/null || true
}

wait_for_pods() {
  info "等待所有 Pod 就绪..."
  
  local timeout=120
  local elapsed=0
  
  while [[ $elapsed -lt $timeout ]]; do
    local not_ready
    not_ready=$(${KUBECTL} get pods -n "${NAMESPACE}" --no-headers 2>/dev/null | awk '$3 != "Running" && $3 != "Completed" { c++ } END { print c+0 }' || true)
    [[ -z "${not_ready}" ]] && not_ready=0
    
    if [[ "$not_ready" -eq 0 ]]; then
      ok "所有业务 Pod 已就绪! ✅"
      echo ""
      ${KUBECTL} get pods -n "${NAMESPACE}" -o wide
      return 0
    fi
    
    echo -ne "\r  等待中... ${elapsed}s / ${timeout}s (${not_ready} 个 Pod 未就绪)"
    sleep 5
    ((elapsed+=5))
  done
  
  echo ""
  warn "部分 Pod 未在 ${timeout}s 内就绪:"
  ${KUBECTL} get pods -n "${NAMESPACE}" -o wide
}

# ============================================================================
# 主入口
# ============================================================================

ACTION="${1:-help}"

case "$ACTION" in
  install)    cmd_install ;;
  upgrade)    cmd_upgrade ;;
  uninstall)  cmd_uninstall ;;
  status)     cmd_status ;;
  lint)       cmd_lint ;;
  template)   cmd_template ;;
  *)
    echo "用法: $0 {install|upgrade|uninstall|status|lint|template}"
    echo ""
    echo "  install    首次安装 (helm install)"
    echo "  upgrade    升级部署 (helm upgrade)"
    echo "  uninstall  卸载 (helm uninstall)"
    echo "  status     查看部署状态"
    echo "  lint       Helm lint 语法检查"
    echo "  template   渲染 Helm 模板到 stdout"
    exit 1
    ;;
esac
