#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

RELEASE_NAME="${RELEASE_NAME:-hci-platform}"
NAMESPACE="${NAMESPACE:-hci-troubleshoot}"
CHART_PATH="${PROJECT_ROOT}/deploy/helm/hci-platform"
VALUES_FILE="${CHART_PATH}/values.yaml"
VALUES_PROD_FILE="${CHART_PATH}/values-prod.yaml"
OVERRIDE_FILE="${OVERRIDE_FILE:-/srv/hci/config/values-prod.override.yaml}"
TIMEOUT="${TIMEOUT:-10m}"
KUBECTL="${KUBECTL:-sudo -n k3s kubectl}"
HELM_KUBECONFIG="${HELM_KUBECONFIG:-${KUBECONFIG:-/etc/rancher/k3s/k3s.yaml}}"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

info()  { echo -e "${BLUE}[INFO]${NC}  $*"; }
ok()    { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; }

ensure_kubectl_access() {
  if ${KUBECTL} version >/dev/null 2>&1; then
    return 0
  fi

  error "无法以非交互方式访问 K3s kubectl"
  error "当前命令: ${KUBECTL}"
  error "请先执行: sudo -v"
  error "或显式指定无需 sudo 的命令，例如: KUBECTL='k3s kubectl' bash scripts/k3s-deploy-prod.sh ..."
  exit 1
}

ensure_helm_kubeconfig_access() {
  if [[ -z "${HELM_KUBECONFIG}" ]]; then
    error "HELM_KUBECONFIG 为空，无法确定 Helm 连接的集群"
    error "请设置 HELM_KUBECONFIG 或 KUBECONFIG"
    exit 1
  fi

  if [[ ! -r "${HELM_KUBECONFIG}" ]]; then
    error "Helm kubeconfig 不可读: ${HELM_KUBECONFIG}"
    error "请确认文件存在并可读，或先执行: sudo -v"
    exit 1
  fi
}

ensure_release_not_pending() {
  local status_output="" release_status=""

  # Release 首次部署前可能不存在，直接跳过保护
  if ! status_output="$(helm status "$RELEASE_NAME" -n "$NAMESPACE" --kubeconfig "$HELM_KUBECONFIG" 2>/dev/null)"; then
    return 0
  fi

  release_status="$(echo "$status_output" | awk -F': ' '/^STATUS:/{print $2}' | tr -d '\r')"
  if [[ "$release_status" == pending-* ]]; then
    error "检测到 Helm release 当前状态为 ${release_status}，存在发布锁，已阻断本次升级"
    error "请先执行回滚解锁，再重试发布："
    error "  helm --kubeconfig ${HELM_KUBECONFIG} -n ${NAMESPACE} history ${RELEASE_NAME}"
    error "  helm --kubeconfig ${HELM_KUBECONFIG} -n ${NAMESPACE} rollback ${RELEASE_NAME} <revision> --wait --timeout ${TIMEOUT}"
    exit 1
  fi
}

usage() {
  cat <<EOF
用法:
  bash scripts/k3s-deploy-prod.sh <action> [args]

Actions:
  lint                  Helm lint (values + values-prod + override)
  template              渲染模板到 stdout
  deploy                helm upgrade --install（生产）
  status                查看 release / pods / svc / ingress
  history               查看 release 历史
  rollback <revision>   回滚到指定 revision

可选环境变量:
  RELEASE_NAME   (默认: hci-platform)
  NAMESPACE      (默认: hci-troubleshoot)
  OVERRIDE_FILE  (默认: /srv/hci/config/values-prod.override.yaml)
  TIMEOUT        (默认: 10m)
  KUBECTL        (默认: sudo k3s kubectl)
  HELM_KUBECONFIG(默认: /etc/rancher/k3s/k3s.yaml 或继承 KUBECONFIG)
EOF
}

require_override() {
  if [[ ! -f "$OVERRIDE_FILE" ]]; then
    error "找不到 OVERRIDE_FILE: $OVERRIDE_FILE"
    error "请先创建生产 override 文件（可参考 deploy/helm/hci-platform/values-prod.override.example.yaml）"
    exit 1
  fi
}

helm_common_args() {
  echo "-f" "$VALUES_FILE" "-f" "$VALUES_PROD_FILE" "-f" "$OVERRIDE_FILE"
}

cmd_lint() {
  require_override
  info "Helm lint (prod)..."
  helm lint "$CHART_PATH" $(helm_common_args)
  ok "lint 通过"
}

cmd_template() {
  require_override
  info "渲染模板 (prod)..."
  helm template "$RELEASE_NAME" "$CHART_PATH" -n "$NAMESPACE" $(helm_common_args)
}

cmd_deploy() {
  require_override
  ensure_helm_kubeconfig_access
  ensure_release_not_pending
  info "部署/升级生产 release: $RELEASE_NAME (ns=$NAMESPACE)"
  helm upgrade --install "$RELEASE_NAME" "$CHART_PATH" \
    --kubeconfig "$HELM_KUBECONFIG" \
    --namespace "$NAMESPACE" \
    --create-namespace \
    $(helm_common_args) \
    --wait \
    --timeout "$TIMEOUT"
  ok "部署完成"
}

cmd_status() {
  ensure_kubectl_access
  ensure_helm_kubeconfig_access
  info "Helm release 状态"
  helm status "$RELEASE_NAME" -n "$NAMESPACE" --kubeconfig "$HELM_KUBECONFIG" || true
  echo
  info "Pod 状态"
  $KUBECTL get pods -n "$NAMESPACE" -o wide || true
  echo
  info "Service 状态"
  $KUBECTL get svc -n "$NAMESPACE" || true
  echo
  info "Ingress 状态"
  $KUBECTL get ingress -n "$NAMESPACE" || true
}

cmd_history() {
  ensure_helm_kubeconfig_access
  helm history "$RELEASE_NAME" -n "$NAMESPACE" --kubeconfig "$HELM_KUBECONFIG"
}

cmd_rollback() {
  local revision="${1:-}"
  if [[ -z "$revision" ]]; then
    error "rollback 需要 revision 参数"
    usage
    exit 1
  fi
  ensure_helm_kubeconfig_access
  info "回滚 $RELEASE_NAME 到 revision=$revision"
  helm rollback "$RELEASE_NAME" "$revision" -n "$NAMESPACE" \
    --kubeconfig "$HELM_KUBECONFIG" \
    --wait --timeout "$TIMEOUT"
  ok "回滚完成"
}

ACTION="${1:-}"

case "$ACTION" in
  lint)
    cmd_lint
    ;;
  template)
    cmd_template
    ;;
  deploy)
    cmd_deploy
    ;;
  status)
    cmd_status
    ;;
  history)
    cmd_history
    ;;
  rollback)
    shift || true
    cmd_rollback "${1:-}"
    ;;
  -h|--help|help|"")
    usage
    ;;
  *)
    error "未知 action: $ACTION"
    usage
    exit 1
    ;;
esac

