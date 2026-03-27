#!/usr/bin/env bash
set -euo pipefail

# 说明：
# - 统一管理 ArgoCD Application 的 apply 入口，防止 local/cloud 目录误操作。
# - 角色来源优先级：--role > HCI_DEVICE_ROLE > argocd namespace label(hci.env.role)。
# - 约束策略：
#   local 目录仅允许 dev 角色；
#   cloud 目录仅允许 staging 角色（Hub-Spoke 控制面）。

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$(dirname "$SCRIPT_DIR")")"

SCOPE=""
ROLE_INPUT=""
YES=false

TRACE_ID="hci-argocd-$(date +%Y%m%d%H%M%S)-$RANDOM"

info()  { echo "[INFO][$TRACE_ID] $*"; }
warn()  { echo "[WARN][$TRACE_ID] $*"; }
error() { echo "[ERROR][$TRACE_ID] $*" >&2; }

usage() {
  cat <<'EOF'
用法:
  bash scripts/ops/argocd-apply-apps.sh --scope <local|cloud> [--role <dev|staging|prod>] [--yes]

参数:
  --scope   目标目录：local 或 cloud
  --role    显式设备角色（可选）。未提供时自动读取 HCI_DEVICE_ROLE 或 argocd 标签 hci.env.role
  --yes     跳过确认提示
  -h,--help 显示帮助

示例:
  bash scripts/ops/argocd-apply-apps.sh --scope local --role dev
  HCI_DEVICE_ROLE=staging bash scripts/ops/argocd-apply-apps.sh --scope cloud
EOF
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --scope)
        SCOPE="${2:-}"
        shift 2
        ;;
      --role)
        ROLE_INPUT="${2:-}"
        shift 2
        ;;
      --yes)
        YES=true
        shift
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      *)
        error "未知参数: $1"
        usage
        exit 1
        ;;
    esac
  done

  if [[ "$SCOPE" != "local" && "$SCOPE" != "cloud" ]]; then
    error "--scope 仅支持 local 或 cloud"
    exit 1
  fi
}

detect_role() {
  local label_role=""

  if [[ -n "${ROLE_INPUT:-}" ]]; then
    echo "$ROLE_INPUT"
    return 0
  fi

  if [[ -n "${HCI_DEVICE_ROLE:-}" ]]; then
    echo "$HCI_DEVICE_ROLE"
    return 0
  fi

  label_role="$(kubectl get ns argocd -o jsonpath='{.metadata.labels.hci\.env\.role}' 2>/dev/null || true)"
  if [[ -n "$label_role" ]]; then
    echo "$label_role"
    return 0
  fi

  echo ""
}

validate_role() {
  local role="$1"

  if [[ "$role" != "dev" && "$role" != "staging" && "$role" != "prod" ]]; then
    error "无法识别设备角色。请使用 --role 指定，或设置 HCI_DEVICE_ROLE，或给 argocd namespace 打标签 hci.env.role"
    exit 1
  fi

  if [[ "$SCOPE" == "local" && "$role" != "dev" ]]; then
    error "角色校验失败：local 目录仅允许 dev 设备执行（当前角色: $role）"
    exit 1
  fi

  if [[ "$SCOPE" == "cloud" && "$role" != "staging" ]]; then
    error "角色校验失败：cloud 目录仅允许 staging 控制面设备执行（当前角色: $role）"
    exit 1
  fi
}

main() {
  parse_args "$@"

  local role
  local context
  local server
  local target_dir

  role="$(detect_role)"
  validate_role "$role"

  context="$(kubectl config current-context 2>/dev/null || echo "unknown")"
  server="$(kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}' 2>/dev/null || echo "unknown")"
  target_dir="$PROJECT_ROOT/deploy/gitops/argo-apps/$SCOPE"

  if [[ ! -d "$target_dir" ]]; then
    error "目录不存在: $target_dir"
    exit 1
  fi

  info "设备角色: $role"
  info "kubectl context: $context"
  info "kubectl server: $server"
  info "目标目录: $target_dir"

  if [[ "$YES" != "true" ]]; then
    read -r -p "确认执行 kubectl apply -f $target_dir ? (yes/no): " ans
    if [[ "$ans" != "yes" ]]; then
      warn "用户取消执行"
      exit 0
    fi
  fi

  kubectl apply -f "$target_dir"
  info "完成: kubectl apply -f $target_dir"
  kubectl get applications -n argocd
}

main "$@"
