#!/usr/bin/env bash
# =============================================================================
# HCI 平台 — K3s 一键发布脚本（构建→导入→更新 tag→部署→验证）
# =============================================================================
# 使用示例:
#   bash scripts/k3s-release.sh
#   bash scripts/k3s-release.sh --services all
#   bash scripts/k3s-release.sh --services customerUI,apiGateway
#   bash scripts/k3s-release.sh --tag 2026.03.15-ssh-bridge --skip-verify
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

# 所有服务键（默认全部）
ALL_SERVICES="apiGateway,caseService,conversationService,schedulerService,customerUI,adminUI,openclaw"

ENVIRONMENT="prod"
SERVICES_CSV="$ALL_SERVICES"   # 默认值 = 全部服务
IMAGE_TAG=""
SKIP_VERIFY=false
SKIP_BUILD=false
SKIP_DEPLOY=false

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

usage() {
  cat <<EOF
用法:
  bash scripts/k3s-release.sh [options]

options:
  --env <prod|dev>               发布环境（默认: prod）
  --services <keys|all>          需要发布的服务（默认: all）
                                   all = 所有服务
                                   多个用逗号分隔: customerUI,apiGateway
  --tag <image_tag>              指定镜像 tag（默认: YYYY.MM.DD-HHMM-<git短sha>）
  --skip-verify                  跳过 scripts/k3s-verify.sh
  --skip-build                   跳过镜像构建和导入（仅更新 tag+部署）
  --skip-deploy                  跳过部署（仅构建+更新 override）
  -h, --help                     显示帮助

服务键可选值:
  all
  apiGateway, caseService, conversationService, schedulerService,
  customerUI, adminUI, openclaw

示例:
  bash scripts/k3s-release.sh                          # 全量发布
  bash scripts/k3s-release.sh --services all           # 全量发布（显式）
  bash scripts/k3s-release.sh --services customerUI    # 只发布 Customer UI
EOF
}

command_exists() { command -v "$1" >/dev/null 2>&1; }

ensure_non_interactive_sudo() {
  if [[ "$KUBECTL" != sudo* ]]; then
    return 0
  fi

  if sudo -n true >/dev/null 2>&1; then
    return 0
  fi

  error "当前发布链路默认使用 sudo 执行 K3s 命令，但未获得非交互 sudo 权限"
  error "请先执行: sudo -v"
  error "或显式指定无需 sudo 的命令，例如: KUBECTL='k3s kubectl' bash scripts/k3s-release.sh ..."
  exit 1
}

require_cmds() {
  local missing=0
  for cmd in git bash helm; do
    command_exists "$cmd" || { error "缺少命令: $cmd"; missing=1; }
  done
  command_exists k3s || { error "缺少命令: k3s"; missing=1; }
  if [[ "$SKIP_BUILD" == false ]]; then
    command_exists docker || { error "缺少命令: docker（构建镜像需要）"; missing=1; }
  fi
  if [[ "$missing" -ne 0 ]]; then
    exit 1
  fi

  return 0
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --env)          ENVIRONMENT="${2:-}";  shift 2 ;;
      --services)     SERVICES_CSV="${2:-}"; shift 2 ;;
      --tag)          IMAGE_TAG="${2:-}";    shift 2 ;;
      --skip-verify)  SKIP_VERIFY=true;  shift ;;
      --skip-build)   SKIP_BUILD=true;   shift ;;
      --skip-deploy)  SKIP_DEPLOY=true;  shift ;;
      -h|--help)      usage; exit 0 ;;
      *) error "未知参数: $1"; usage; exit 1 ;;
    esac
  done

  # all 展开为完整列表
  if [[ "$SERVICES_CSV" == "all" ]]; then
    SERVICES_CSV="$ALL_SERVICES"
  fi

  if [[ "$ENVIRONMENT" != "prod" && "$ENVIRONMENT" != "dev" ]]; then
    error "--env 仅支持 prod 或 dev"
    exit 1
  fi
}

gen_default_tag() {
  local short_sha
  short_sha="$(git -C "$PROJECT_ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)"
  date +"%Y.%m.%d-%H%M-${short_sha}"
}

resolve_override_file() {
  local prod_srv="/srv/hci/config/values-prod.override.yaml"
  local prod_local="$PROJECT_ROOT/.local/values-prod.override.yaml"

  if [[ "$ENVIRONMENT" == "prod" ]]; then
    if [[ -f "$prod_srv" ]]; then
      echo "$prod_srv"
      return 0
    fi

    if [[ -f "$prod_local" ]]; then
      echo "$prod_local"
      return 0
    fi

    error "prod 环境未找到 override 文件（/srv 或 .local）"; exit 1
  fi

  if [[ -f "$prod_local" ]]; then
    echo "$prod_local"
    return 0
  fi

  if [[ -f "$prod_srv" ]]; then
    echo "$prod_srv"
    return 0
  fi

  error "dev 环境未找到 override 文件（/srv 或 .local）"; exit 1
}

validate_services() {
  IFS=',' read -r -a SELECTED_SERVICES <<< "$1"
  if [[ "${#SELECTED_SERVICES[@]}" -eq 0 ]]; then
    error "--services 不能为空"
    exit 1
  fi

  for svc in "${SELECTED_SERVICES[@]}"; do
    case "$svc" in
      apiGateway|caseService|conversationService|schedulerService|\
      customerUI|adminUI|openclaw) ;;
      *) error "未知服务键: $svc，可选值请查看 --help"; exit 1 ;;
    esac
  done

  return 0
}

update_service_tag_in_override() {
  local file="$1" key="$2" tag="$3" tmp
  tmp="$(mktemp)"
  awk -v key="$key" -v tag="$tag" '
    BEGIN { in_block=0 }
    {
      if ($0 ~ "^" key ":$") { in_block=1; print; next }
      if (in_block && $0 ~ /^[A-Za-z0-9_]+:$/ && $0 !~ "^image:$") { in_block=0 }
      if (in_block && $0 ~ /^[[:space:]]+tag:[[:space:]]*"[^"]*"/) {
        sub(/tag:[[:space:]]*"[^"]*"/, "tag: \"" tag "\"")
      }
      print
    }
  ' "$file" > "$tmp"
  mv "$tmp" "$file"
}

update_openclaw_related_config() {
  local file="$1" tag="$2" tmp
  tmp="$(mktemp)"
  sed -E "s#(openclawImage:[[:space:]]*\"hci-openclaw:)[^\"]+(\")#\1${tag}\2#g;
          s#(hci-openclaw:)[0-9A-Za-z._-]+#\1${tag}#g" "$file" > "$tmp"
  mv "$tmp" "$file"
}

apply_tag_updates() {
  local override_file="$1" tag="$2" svc
  for svc in "${SELECTED_SERVICES[@]}"; do
    info "更新 override: ${svc}.image.tag -> ${tag}"
    update_service_tag_in_override "$override_file" "$svc" "$tag"
    if [[ "$svc" == "openclaw" ]]; then
      info "同步更新 openclaw 关联配置"
      update_openclaw_related_config "$override_file" "$tag"
    fi
  done
}

# =============================================================================
# terminal_bridge.exe 检查
# 构建 customerUI 前必须确认 exe 已就绪，否则下载按钮 404
# =============================================================================
check_terminal_bridge_exe() {
  local exe_path="${PROJECT_ROOT}/frontend/customer/public/downloads/terminal_bridge.exe"
  local need_customer_ui=false

  for svc in "${SELECTED_SERVICES[@]}"; do
    if [[ "$svc" == "customerUI" ]]; then
      need_customer_ui=true
      break
    fi
  done

  if [[ "$need_customer_ui" == false ]]; then
    return 0
  fi

  if [[ -f "$exe_path" ]]; then
    ok "terminal_bridge.exe 已就绪: $(du -sh "$exe_path" | cut -f1) — ${exe_path}"
    return 0
  fi

  error "═══════════════════════════════════════════════════════════"
  error " terminal_bridge.exe 未找到，无法继续发布"
  error " 期望路径: ${exe_path}"
  error ""
  error " 构建步骤（在 Windows 上执行）:"
  error "   cd terminal_bridge"
  error "   build_windows.bat"
  error "   copy dist\\terminal_bridge.exe ..\\ \\"
  error "     frontend\\customer\\public\\downloads\\"
  error ""
  error " 或者先不发布 customerUI:"
  error "   bash scripts/k3s-release.sh --services apiGateway,caseService,..."
  error "═══════════════════════════════════════════════════════════"
  exit 1
}

build_and_import_images() {
  local tag="$1" repos=() svc repos_csv
  for svc in "${SELECTED_SERVICES[@]}"; do
    repos+=("$(service_key_to_repository "$svc")")
  done
  repos_csv="$(IFS=','; echo "${repos[*]}")"

  info "开始构建并导入 K3s 镜像 (tag=${tag})"
  info "仅构建: ${repos_csv}"
  (
    cd "$PROJECT_ROOT"
    IMAGE_TAG="$tag" BUILD_ONLY_IMAGES="$repos_csv" bash scripts/k3s-build.sh
  )
  ok "镜像构建与导入完成"
}

deploy_to_k3s() {
  local override_file="$1"
  if [[ "$ENVIRONMENT" == "prod" ]]; then
    info "执行生产部署 (Helm upgrade --install)"
    ( cd "$PROJECT_ROOT"; OVERRIDE_FILE="$override_file" HELM_KUBECONFIG="$HELM_KUBECONFIG" bash scripts/k3s-deploy-prod.sh deploy )
  else
    info "执行开发部署 (Helm upgrade)"
    ( cd "$PROJECT_ROOT"; bash scripts/k3s-deploy.sh --env dev upgrade )
  fi
  ok "Helm 部署完成"
}

service_key_to_deploy_name() {
  case "$1" in
    apiGateway)          echo "api-gateway" ;;
    caseService)         echo "case-service" ;;
    conversationService) echo "conversation-service" ;;
    schedulerService)    echo "scheduler-service" ;;
    customerUI)          echo "customer-ui" ;;
    adminUI)             echo "admin-ui" ;;
    openclaw)            echo "openclaw" ;;
    *) return 1 ;;
  esac
}

service_key_to_repository() {
  case "$1" in
    apiGateway)          echo "hci-api-gateway" ;;
    caseService)         echo "hci-case-service" ;;
    conversationService) echo "hci-conversation-service" ;;
    schedulerService)    echo "hci-scheduler-service" ;;
    customerUI)          echo "hci-customer-ui" ;;
    adminUI)             echo "hci-admin-ui" ;;
    openclaw)            echo "hci-openclaw" ;;
    *) return 1 ;;
  esac
}

verify_image_consistency() {
  local namespace="hci-troubleshoot" svc deploy_name repository expected actual
  info "开始校验集群镜像版本一致性"
  for svc in "${SELECTED_SERVICES[@]}"; do
    deploy_name="$(service_key_to_deploy_name "$svc")"
    repository="$(service_key_to_repository "$svc")"
    expected="${repository}:${IMAGE_TAG}"
    actual="$($KUBECTL -n "$namespace" get deploy "$deploy_name" \
      -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null || echo 'NOT_FOUND')"
    if [[ "$actual" != "$expected" ]]; then
      error "镜像校验失败: ${deploy_name}"
      error "  期望: ${expected}"
      error "  实际: ${actual}"
      return 1
    fi
    ok "镜像校验通过: ${deploy_name} → ${actual}"
  done
}

run_post_verify() {
  info "执行集群部署验证脚本"
  ( cd "$PROJECT_ROOT"; bash scripts/k3s-verify.sh )
}

print_release_summary() {
  echo ""
  ok "发布流程完成 ✅"
  echo "  环境:     ${ENVIRONMENT}"
  echo "  镜像 tag: ${IMAGE_TAG}"
  echo "  服务:     ${SERVICES_CSV}"
  echo "  override: ${OVERRIDE_FILE}"
  echo ""
  info "验证建议:"
  echo "  $KUBECTL -n hci-troubleshoot get deploy customer-ui -o wide"
  echo "  访问 Custom UI 后强刷缓存 (Ctrl+Shift+R)"
  echo "  点击「终端」按钮验证 Bridge 检测和下载流程"
}

main() {
  parse_args "$@"
  require_cmds
  ensure_non_interactive_sudo
  validate_services "$SERVICES_CSV"

  if [[ -z "$IMAGE_TAG" ]]; then
    IMAGE_TAG="$(gen_default_tag)"
  fi

  OVERRIDE_FILE="$(resolve_override_file)"

  info "发布参数确认"
  echo "  ENVIRONMENT = ${ENVIRONMENT}"
  echo "  IMAGE_TAG   = ${IMAGE_TAG}"
  echo "  SERVICES    = ${SERVICES_CSV}"
  echo "  OVERRIDE    = ${OVERRIDE_FILE}"
  echo ""

  apply_tag_updates "$OVERRIDE_FILE" "$IMAGE_TAG"

  if [[ "$SKIP_BUILD" == false ]]; then
    check_terminal_bridge_exe   # exe 不存在时强制阻断 (exit 1)
    build_and_import_images "$IMAGE_TAG"
  else
    warn "已跳过构建导入 (--skip-build)"
  fi

  if [[ "$SKIP_DEPLOY" == false ]]; then
    deploy_to_k3s "$OVERRIDE_FILE"
  else
    warn "已跳过部署 (--skip-deploy)"
  fi

  verify_image_consistency

  if [[ "$SKIP_VERIFY" == false ]]; then
    run_post_verify
  else
    warn "已跳过综合验证 (--skip-verify)"
  fi

  print_release_summary
}

main "$@"
