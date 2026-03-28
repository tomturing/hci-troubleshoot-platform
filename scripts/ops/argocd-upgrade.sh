#!/usr/bin/env bash
set -euo pipefail

ARGOCD_VERSION="${ARGOCD_VERSION:-v3.3.6}"
KUBECTL="${KUBECTL:-kubectl}"
WATCHDOG_MANIFEST="${WATCHDOG_MANIFEST:-deploy/gitops/argocd-ops/argocd-repo-server-copyutil-watchdog.yaml}"

TRACE_ID="hci-argocd-upgrade-$(date +%Y%m%d%H%M%S)-$RANDOM"

info() {
  echo "[INFO][${TRACE_ID}] $*"
}

warn() {
  echo "[WARN][${TRACE_ID}] $*" >&2
}

error() {
  echo "[ERROR][${TRACE_ID}] $*" >&2
}

usage() {
  cat <<'EOF'
用法：
  bash scripts/ops/argocd-upgrade.sh

可选环境变量：
  ARGOCD_VERSION   ArgoCD 版本（默认：v3.3.6）
  KUBECTL          kubectl 命令（默认：kubectl）
  WATCHDOG_MANIFEST watchdog 清单路径（默认：deploy/gitops/argocd-ops/argocd-repo-server-copyutil-watchdog.yaml）

示例：
  ARGOCD_VERSION=v3.3.6 bash scripts/ops/argocd-upgrade.sh
  KUBECTL='sudo -n k3s kubectl' bash scripts/ops/argocd-upgrade.sh
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

if ! command -v "${KUBECTL%% *}" >/dev/null 2>&1; then
  error "未找到 kubectl 命令：${KUBECTL}"
  exit 1
fi

INSTALL_URL="https://raw.githubusercontent.com/argoproj/argo-cd/${ARGOCD_VERSION}/manifests/install.yaml"

info "开始升级 ArgoCD 到 ${ARGOCD_VERSION}"
info "安装清单：${INSTALL_URL}"

${KUBECTL} create namespace argocd --dry-run=client -o yaml | ${KUBECTL} apply -f -
if ! ${KUBECTL} apply -n argocd -f "${INSTALL_URL}"; then
  warn "常规 apply 失败，尝试 server-side apply（常见于 CRD 注解过长）"
  ${KUBECTL} apply --server-side -n argocd -f "${INSTALL_URL}"
fi

info "等待核心组件就绪"
${KUBECTL} -n argocd rollout status deployment/argocd-server --timeout=300s
${KUBECTL} -n argocd rollout status deployment/argocd-repo-server --timeout=300s
${KUBECTL} -n argocd rollout status statefulset/argocd-application-controller --timeout=300s

if [[ -f "${WATCHDOG_MANIFEST}" ]]; then
  info "应用 watchdog 清单：${WATCHDOG_MANIFEST}"
  ${KUBECTL} apply -f "${WATCHDOG_MANIFEST}"
else
  warn "未找到 watchdog 清单，跳过：${WATCHDOG_MANIFEST}"
fi

info "当前 ArgoCD 组件镜像："
${KUBECTL} -n argocd get deploy argocd-server argocd-repo-server -o custom-columns=NAME:.metadata.name,IMAGE:.spec.template.spec.containers[*].image
${KUBECTL} -n argocd get statefulset argocd-application-controller -o custom-columns=NAME:.metadata.name,IMAGE:.spec.template.spec.containers[*].image

info "完成：ArgoCD 已升级到 ${ARGOCD_VERSION}（按 manifest 版本）并应用 watchdog（如存在）"
