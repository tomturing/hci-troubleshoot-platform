#!/usr/bin/env bash
# =============================================================================
# HCI 平台 — K3s 一键发布脚本（构建→导入→更新 tag→部署→验证）
# =============================================================================
# 目标：保证代码修改后能及时、正确地生效到集群中。
#
# 使用示例：
#   bash scripts/k3s-release.sh
#   bash scripts/k3s-release.sh --env prod --services customerUI,apiGateway
#   bash scripts/k3s-release.sh --tag 2026.03.15-ssh-sidebar --skip-verify
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

ENVIRONMENT="prod"
SERVICES_CSV="apiGateway,caseService,conversationService,schedulerService,customerUI,adminUI,openclaw"
IMAGE_TAG=""
SKIP_VERIFY=false
SKIP_BUILD=false
SKIP_DEPLOY=false

KUBECTL="${KUBECTL:-sudo k3s kubectl}"

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
  cat <<'EOF'
用法:
  bash scripts/k3s-release.sh [options]

options:
  --env <prod|dev>               发布环境（默认: prod）
  --services <a,b,c>             需要更新 tag 的服务键（默认全部核心服务）
  --tag <image_tag>              指定镜像 tag（默认: YYYY.MM.DD-HHMM-<git短sha>）
  --skip-verify                  跳过 scripts/k3s-verify.sh
  --skip-build                   跳过镜像构建和导入（仅更新 tag+部署）
  --skip-deploy                  跳过部署（仅构建+更新 override）
  -h, --help                     显示帮助

服务键可选值:
  apiGateway, caseService, conversationService, schedulerService,
  customerUI, adminUI, openclaw
EOF
}

command_exists() {
  command -v "$1" >/dev/null 2>&1
}

require_cmds() {
  local missing=0
  for cmd in git bash helm; do
    if ! command_exists "$cmd"; then
      error "缺少命令: $cmd"
      missing=1
    fi
  done

  if ! command_exists k3s; then
    error "缺少命令: k3s"
    missing=1
  fi

  if [[ "$SKIP_BUILD" == false ]] && ! command_exists docker; then
    error "缺少命令: docker（构建镜像需要）"
    missing=1
  fi

  if [[ "$missing" -ne 0 ]]; then
    exit 1
  fi
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --env)
        ENVIRONMENT="${2:-}"
        shift 2
        ;;
      --services)
        SERVICES_CSV="${2:-}"
        shift 2
        ;;
      --tag)
        IMAGE_TAG="${2:-}"
        shift 2
        ;;
      --skip-verify)
        SKIP_VERIFY=true
        shift
        ;;
      --skip-build)
        SKIP_BUILD=true
        shift
        ;;
      --skip-deploy)
        SKIP_DEPLOY=true
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
    error "prod 环境未找到 override 文件（/srv 或 .local）"
    exit 1
  fi

  # dev 环境优先 .local
  if [[ -f "$prod_local" ]]; then
    echo "$prod_local"
    return 0
  fi

  # dev 兜底仍可使用 /srv
  if [[ -f "$prod_srv" ]]; then
    echo "$prod_srv"
    return 0
  fi

  error "dev 环境也未找到 override 文件（/srv 或 .local）"
  exit 1
}

validate_services() {
  local raw="$1"
  IFS=',' read -r -a SELECTED_SERVICES <<< "$raw"

  if [[ "${#SELECTED_SERVICES[@]}" -eq 0 ]]; then
    error "--services 不能为空"
    exit 1
  fi

  for svc in "${SELECTED_SERVICES[@]}"; do
    case "$svc" in
      apiGateway|caseService|conversationService|schedulerService|customerUI|adminUI|openclaw)
        ;;
      *)
        error "未知服务键: $svc"
        exit 1
        ;;
    esac
  done
}

update_service_tag_in_override() {
  local file="$1"
  local key="$2"
  local tag="$3"
  local tmp
  tmp="$(mktemp)"

  awk -v key="$key" -v tag="$tag" '
    BEGIN { in_block = 0 }
    {
      # 命中顶层键
      if ($0 ~ "^" key ":$") {
        in_block = 1
        print $0
        next
      }

      # 离开当前顶层键
      if (in_block == 1 && $0 ~ /^[A-Za-z0-9_]+:$/ && $0 !~ "^image:$") {
        in_block = 0
      }

      # 仅在对应服务块内替换 image.tag
      if (in_block == 1 && $0 ~ /^[[:space:]]+tag:[[:space:]]*"[^"]*"/) {
        sub(/tag:[[:space:]]*"[^"]*"/, "tag: \"" tag "\"")
      }

      print $0
    }
  ' "$file" > "$tmp"

  mv "$tmp" "$file"
}

update_openclaw_related_config() {
  local file="$1"
  local tag="$2"
  local tmp
  tmp="$(mktemp)"

  # 保持原有字段格式，只更新 openclaw 镜像 tag
  sed -E "s#(openclawImage:[[:space:]]*\"hci-openclaw:)[^\"]+(\")#\1${tag}\2#g; s#(hci-openclaw:)[0-9A-Za-z._-]+#\1${tag}#g" "$file" > "$tmp"
  mv "$tmp" "$file"
}

apply_tag_updates() {
  local override_file="$1"
  local tag="$2"
  local svc

  for svc in "${SELECTED_SERVICES[@]}"; do
    info "更新 override: ${svc}.image.tag -> ${tag}"
    update_service_tag_in_override "$override_file" "$svc" "$tag"

    if [[ "$svc" == "openclaw" ]]; then
      info "同步更新 openclaw 关联配置（openclawImage / assistantRegistryJson）"
      update_openclaw_related_config "$override_file" "$tag"
    fi
  done
}

# =============================================================================
# 构建前检查：确认 terminal_bridge.exe 已就位
# customerUI 镜像构建时 Vite 会将 public/ 原样打包进 dist/，
# 若 exe 文件缺失，构建可以成功但下载按钮会 404。
# =============================================================================
check_terminal_bridge_asset() {
  local exe_path="${PROJECT_ROOT}/frontend/customer/public/downloads/terminal_bridge.exe"

  # 仅当本次构建包含 customerUI 时才检查
  local need_check=false
  for svc in "${SELECTED_SERVICES[@]}"; do
    [[ "$svc" == "customerUI" ]] && need_check=true && break
  done

  [[ "$need_check" == false ]] && return 0

  if [[ ! -f "$exe_path" ]]; then
    warn "==========================================================="
    warn " terminal_bridge.exe 未找到，下载按钮发布后将返回 404！"
    warn ""
    warn " 请在继续前将编译好的 exe 放入："
    warn "   ${exe_path}"
    warn ""
    warn " 如果暂时没有 exe，可忽略此警告继续发布，"
    warn " 但用户点击"下载并打开"时会看到 404 错误。"
    warn "==========================================================="
    echo ""
    # 非阻塞警告，不退出，由发布者决定是否继续
  else
    local size
    size=$(du -h "$exe_path" | cut -f1)
    ok "terminal_bridge.exe 已就位（${size}），将随镜像构建打包"
  fi
}

build_and_import_images() {
  local tag="$1"
  local repos=()
  local svc

  for svc in "${SELECTED_SERVICES[@]}"; do
    repos+=("$(service_key_to_repository "$svc")")
  done

  local repos_csv
  repos_csv="$(IFS=','; echo "${repos[*]}")"

  info "开始构建并导入 K3s 镜像，tag=${tag}"
  (
    cd "$PROJECT_ROOT"
    IMAGE_TAG="$tag" BUILD_ONLY_IMAGES="$repos_csv" bash scripts/k3s-build.sh
  )
  ok "镜像构建与导入完成"
}

deploy_to_k3s() {
  local override_file="$1"

  if [[ "$ENVIRONMENT" == "prod" ]]; then
    info "执行生产部署（Helm upgrade --install）"
    (
      cd "$PROJECT_ROOT"
      OVERRIDE_FILE="$override_file" bash scripts/k3s-deploy-prod.sh deploy
    )
  else
    info "执行开发部署（Helm upgrade）"
    (
      cd "$PROJECT_ROOT"
      bash scripts/k3s-deploy.sh --env dev upgrade
    )
  fi

  ok "Helm 部署完成"
}

service_key_to_deploy_name() {
  case "$1" in
    apiGateway) echo "api-gateway" ;;
    caseService) echo "case-service" ;;
    conversationService) echo "conversation-service" ;;
    schedulerService) echo "scheduler-service" ;;
    customerUI) echo "customer-ui" ;;
    adminUI) echo "admin-ui" ;;
    openclaw) echo "openclaw" ;;
    *) return 1 ;;
  esac
}

service_key_to_repository() {
  case "$1" in
    apiGateway) echo "hci-api-gateway" ;;
    caseService) echo "hci-case-service" ;;
    conversationService) echo "hci-conversation-service" ;;
    schedulerService) echo "hci-scheduler-service" ;;
    customerUI) echo "hci-customer-ui" ;;
    adminUI) echo "hci-admin-ui" ;;
    openclaw) echo "hci-openclaw" ;;
    *) return 1 ;;
  esac
}

verify_image_consistency() {
  local namespace="hci-troubleshoot"
  local svc deploy_name repository expected actual

  info "开始校验集群镜像版本一致性"

  for svc in "${SELECTED_SERVICES[@]}"; do
    deploy_name="$(service_key_to_deploy_name "$svc")"
    repository="$(service_key_to_repository "$svc")"
    expected="${repository}:${IMAGE_TAG}"

    actual="$($KUBECTL -n "$namespace" get deploy "$deploy_name" -o jsonpath='{.spec.template.spec.containers[0].image}')"

    if [[ "$actual" != "$expected" ]]; then
      error "镜像校验失败: ${deploy_name}"
      error "  期望: ${expected}"
      error "  实际: ${actual}"
      return 1
    fi

    ok "镜像校验通过: ${deploy_name} -> ${actual}"
  done
}

run_post_verify() {
  info "执行集群部署验证脚本"
  (
    cd "$PROJECT_ROOT"
    bash scripts/k3s-verify.sh
  )
}

print_release_summary() {
  echo ""
  ok "发布流程完成 ✅"
  echo "  - 环境:      ${ENVIRONMENT}"
  echo "  - 镜像 tag:  ${IMAGE_TAG}"
  echo "  - 服务:      ${SERVICES_CSV}"
  echo "  - override:  ${OVERRIDE_FILE}"
  echo ""
  info "验证建议："
  echo "  1) $KUBECTL -n hci-troubleshoot get deploy customer-ui -o wide"
  echo "  2) 访问 Custom UI 页面后强刷缓存（Ctrl+Shift+R）"
  echo "  3) 点击右上角"终端"按钮："
  echo "       - Bridge 未运行 → 应弹出下载提示，点击可下载 terminal_bridge.exe"
  echo "       - Bridge 运行中 → 应直接打开 SSH 侧边栏"
}

main() {
  parse_args "$@"
  require_cmds

  validate_services "$SERVICES_CSV"

  if [[ -z "$IMAGE_TAG" ]]; then
    IMAGE_TAG="$(gen_default_tag)"
  fi

  OVERRIDE_FILE="$(resolve_override_file)"

  info "发布参数确认"
  echo "  - ENVIRONMENT = ${ENVIRONMENT}"
  echo "  - IMAGE_TAG   = ${IMAGE_TAG}"
  echo "  - SERVICES    = ${SERVICES_CSV}"
  echo "  - OVERRIDE    = ${OVERRIDE_FILE}"
  echo ""

  # 构建前检查 terminal_bridge.exe 是否就位
  check_terminal_bridge_asset

  # 先更新 override，确保发布配置和目标一致
  apply_tag_updates "$OVERRIDE_FILE" "$IMAGE_TAG"

  if [[ "$SKIP_BUILD" == false ]]; then
    build_and_import_images "$IMAGE_TAG"
  else
    warn "已跳过构建导入（--skip-build）"
  fi

  if [[ "$SKIP_DEPLOY" == false ]]; then
    deploy_to_k3s "$OVERRIDE_FILE"
  else
    warn "已跳过部署（--skip-deploy）"
  fi

  verify_image_consistency

  if [[ "$SKIP_VERIFY" == false ]]; then
    run_post_verify
  else
    warn "已跳过综合验证（--skip-verify）"
  fi

  print_release_summary
}

main "$@"
