#!/bin/bash
# =============================================================================
# HCI 平台 — Helm 部署管理脚本
# =============================================================================
# 使用方法:
#   bash scripts/k3s-deploy.sh [--env dev|prod] install    # 首次安装
#   bash scripts/k3s-deploy.sh [--env dev|prod] upgrade    # 升级
#   bash scripts/k3s-deploy.sh uninstall                   # 卸载
#   bash scripts/k3s-deploy.sh status                      # 查看状态
#   bash scripts/k3s-deploy.sh lint                        # Helm lint 检查
#   bash scripts/k3s-deploy.sh template                    # 渲染模板到 stdout
#
# --env 参数说明:
#   dev  (默认) values.yaml + values-dev.yaml
#              imagePullPolicy: Never, 单副本, DEBUG 日志
#              Ingress 域名: <本机IP>.nip.io
#   prod        values.yaml + values-prod.yaml + .local/values-prod.override.yaml
#              imagePullPolicy: IfNotPresent, 多副本, HPA, WARNING 日志
#              Ingress 域名: 取自 override 文件中的 global.domain
#              注意: .local/values-prod.override.yaml 必须存在
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
VALUES_PROD_FILE="${CHART_PATH}/values-prod.yaml"
# 本地覆盖文件（含实际密钥，不入 git），放在 .local/values-prod.override.yaml
VALUES_OVERRIDE_FILE="${PROJECT_ROOT}/.local/values-prod.override.yaml"
KUBECTL="sudo k3s kubectl"

# ============================================================================
# 解析 --env 参数（必须在其他参数之前）
# ============================================================================
ENV="auto"   # 未显式指定时根据 override 文件是否存在自动决定
ARGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --env)
      ENV="${2:?--env 需要参数: dev 或 prod}"
      if [[ "$ENV" != "dev" && "$ENV" != "prod" ]]; then
        echo "错误: --env 只接受 dev 或 prod" >&2; exit 1
      fi
      shift 2
      ;;
    *) ARGS+=("$1"); shift ;;
  esac
done
set -- "${ARGS[@]+"${ARGS[@]}"}"

# 自动推断：生产机有 override 文件 → prod；开发机没有 → dev
if [[ "$ENV" == "auto" ]]; then
  if [[ -f "${VALUES_OVERRIDE_FILE}" ]]; then
    ENV="prod"
    echo -e "\033[1;33m[WARN]\033[0m 检测到 ${VALUES_OVERRIDE_FILE}，自动切换为 prod 环境。如需强制 dev 请传 --env dev"
  else
    ENV="dev"
  fi
fi

# 根据环境构建 values 参数链
_build_values_args() {
  local -n _out=$1
  if [[ "$ENV" == "prod" ]]; then
    if [[ ! -f "${VALUES_OVERRIDE_FILE}" ]]; then
      error "生产环境部署需要 ${VALUES_OVERRIDE_FILE}，文件不存在！"
      error "请参考 ${CHART_PATH}/values-prod.override.example.yaml 创建该文件。"
      exit 1
    fi
    _out=("-f" "${VALUES_FILE}" "-f" "${VALUES_PROD_FILE}" "-f" "${VALUES_OVERRIDE_FILE}")
    info "环境: prod — values.yaml + values-prod.yaml + values-prod.override.yaml"
  else
    _out=("-f" "${VALUES_FILE}" "-f" "${VALUES_DEV_FILE}")
    info "环境: dev  — values.yaml + values-dev.yaml"
  fi
}

# 获取 domain 参数：prod 模式从 override 文件读取，dev 模式自动设置 nip.io
_build_domain_arg() {
  local -n _dout=$1
  if [[ "$ENV" == "prod" ]]; then
    # prod 模式：domain 必须在 override 文件中显式声明，不自动注入
    _dout=()
  else
    # dev 模式且无 override 文件时自动设置开发域名
    if [[ ! -f "${VALUES_OVERRIDE_FILE}" ]]; then
      local WSL_IP; WSL_IP=$(hostname -I | awk '{print $1}')
      _dout=("--set" "global.domain=${WSL_IP}.nip.io")
    else
      _dout=()
    fi
  fi
}

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
  local VALUES_ARGS=()
  _build_values_args VALUES_ARGS
  helm lint "${CHART_PATH}" "${VALUES_ARGS[@]}"
  ok "Lint 通过! ✅"
}

cmd_template() {
  info "渲染 Helm 模板..."
  local VALUES_ARGS=()
  _build_values_args VALUES_ARGS
  local DOMAIN_ARG=()
  _build_domain_arg DOMAIN_ARG
  helm template "${RELEASE_NAME}" "${CHART_PATH}" \
    "${VALUES_ARGS[@]}" \
    "${DOMAIN_ARG[@]+"${DOMAIN_ARG[@]}"}"
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

  local VALUES_ARGS=()
  _build_values_args VALUES_ARGS
  local DOMAIN_ARG=()
  _build_domain_arg DOMAIN_ARG

  # Helm install
  helm install "${RELEASE_NAME}" "${CHART_PATH}" \
    "${VALUES_ARGS[@]}" \
    "${DOMAIN_ARG[@]+"${DOMAIN_ARG[@]}"}" \
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

  local VALUES_ARGS=()
  _build_values_args VALUES_ARGS
  local DOMAIN_ARG=()
  _build_domain_arg DOMAIN_ARG

  helm upgrade "${RELEASE_NAME}" "${CHART_PATH}" \
    "${VALUES_ARGS[@]}" \
    "${DOMAIN_ARG[@]+"${DOMAIN_ARG[@]}"}" \
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
    echo "用法: $0 [--env dev|prod] {install|upgrade|uninstall|status|lint|template}"
    echo ""
    echo "  --env dev   (默认) 开发环境: values-dev.yaml + 可选 override，自动注入 nip.io 域名"
    echo "  --env prod  生产环境: values-prod.yaml + .local/values-prod.override.yaml（必须存在）"
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
