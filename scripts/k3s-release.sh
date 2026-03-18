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
ALL_SERVICES="apiGateway,caseService,conversationService,schedulerService,kbService,customerUI,adminUI,openclaw"

ENVIRONMENT="prod"
SERVICES_CSV="$ALL_SERVICES"   # 默认值 = 全部服务
IMAGE_TAG=""
SKIP_VERIFY=false
SKIP_BUILD=false
SKIP_DEPLOY=false

default_kubectl_cmd() {
  if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
    echo "k3s kubectl"
  else
    echo "sudo -n k3s kubectl"
  fi
}

KUBECTL="${KUBECTL:-$(default_kubectl_cmd)}"
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
  apiGateway, caseService, conversationService, schedulerService, kbService,
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
  error "或在 root 会话执行，或显式指定无需 sudo 的命令，例如: KUBECTL='k3s kubectl' bash scripts/k3s-release.sh ..."
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
      kbService|customerUI|adminUI|openclaw) ;;
      *) error "未知服务键: $svc，可选值请查看 --help"; exit 1 ;;
    esac
  done

  return 0
}

service_selected() {
  local target="$1" svc
  for svc in "${SELECTED_SERVICES[@]}"; do
    if [[ "$svc" == "$target" ]]; then
      return 0
    fi
  done
  return 1
}

has_scheduler_related_changes() {
  local dirty_changes recent_changes

  dirty_changes="$(git -C "$PROJECT_ROOT" status --porcelain -- \
    backend/scheduler-service \
    scripts/k3s-verify.sh 2>/dev/null || true)"
  if [[ -n "$dirty_changes" ]]; then
    return 0
  fi

  if git -C "$PROJECT_ROOT" rev-parse --verify HEAD~1 >/dev/null 2>&1; then
    recent_changes="$(git -C "$PROJECT_ROOT" diff --name-only HEAD~1 HEAD -- \
      backend/scheduler-service \
      scripts/k3s-verify.sh 2>/dev/null || true)"
    if [[ -n "$recent_changes" ]]; then
      return 0
    fi
  fi

  return 1
}

warn_scheduler_release_dependency() {
  # 仅在发布 customerUI / conversationService 且未发布 schedulerService 时提示
  if service_selected "schedulerService"; then
    return 0
  fi

  if ! service_selected "customerUI" && ! service_selected "conversationService"; then
    return 0
  fi

  if has_scheduler_related_changes; then
    warn "检测到 scheduler 相关变更，但本次未包含 schedulerService 发布"
    warn "可能导致线上继续运行旧的 Pod 创建逻辑（例如 productionclaw 配置注入缺失）"
    warn "建议命令: bash scripts/k3s-release.sh --services ${SERVICES_CSV},schedulerService"
  fi
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
    ( cd "$PROJECT_ROOT"; IMAGE_TAG="$IMAGE_TAG" OVERRIDE_FILE="$override_file" HELM_KUBECONFIG="$HELM_KUBECONFIG" bash scripts/k3s-deploy-prod.sh deploy )
  else
    info "执行开发部署 (Helm upgrade)"
    ( cd "$PROJECT_ROOT"; IMAGE_TAG="$IMAGE_TAG" HELM_KUBECONFIG="$HELM_KUBECONFIG" KUBECONFIG="$HELM_KUBECONFIG" bash scripts/k3s-deploy.sh --env dev upgrade )
  fi
  ok "Helm 部署完成"
}

service_key_to_deploy_name() {
  case "$1" in
    apiGateway)          echo "api-gateway" ;;
    caseService)         echo "case-service" ;;
    conversationService) echo "conversation-service" ;;
    schedulerService)    echo "scheduler-service" ;;
    kbService)           echo "kb-service" ;;
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
    kbService)           echo "hci-kb-service" ;;
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

postgres_ready() {
  $KUBECTL -n hci-troubleshoot get pod postgres-0 >/dev/null 2>&1
}

run_db_migrations() {
  local migration_file="$PROJECT_ROOT/database/migrate_evaluation_v1.sql"

  if [[ ! -f "$migration_file" ]]; then
    warn "未找到迁移脚本，跳过数据库迁移: ${migration_file}"
    return 0
  fi

  if ! postgres_ready; then
    warn "未检测到 postgres-0，暂跳过数据库迁移（首次安装场景可忽略）"
    return 0
  fi

  info "执行数据库迁移: migrate_evaluation_v1.sql"
  $KUBECTL exec -i -n hci-troubleshoot postgres-0 -- \
    psql -U hci_admin -d hci_troubleshoot < "$migration_file"
  ok "数据库迁移执行完成"
}

validate_db_schema_contract() {
  local close_reason_col_count

  if ! postgres_ready; then
    warn "未检测到 postgres-0，跳过数据库契约校验"
    return 0
  fi

  close_reason_col_count="$($KUBECTL exec -n hci-troubleshoot postgres-0 -- \
    psql -U hci_admin -d hci_troubleshoot -tAc \
    "SELECT COUNT(1) FROM information_schema.columns WHERE table_schema='public' AND table_name='case' AND column_name='close_reason';" \
    | tr -d '[:space:]')"

  if [[ "$close_reason_col_count" != "1" ]]; then
    error "数据库契约校验失败: case.close_reason 字段缺失"
    return 1
  fi

  ok "数据库契约校验通过: case.close_reason 存在"
}

verify_no_latest_business_images() {
  local images latest_images

  images="$($KUBECTL -n hci-troubleshoot get deploy,statefulset -o jsonpath='{range .items[*].spec.template.spec.initContainers[*]}{.image}{"\n"}{end}{range .items[*].spec.template.spec.containers[*]}{.image}{"\n"}{end}' 2>/dev/null || true)"

  latest_images="$(echo "$images" | grep -E '(^|/)hci-[^:]+:latest$' || true)"
  if [[ -n "$latest_images" ]]; then
    error "检测到业务镜像仍使用 latest，发布中止"
    echo "$latest_images" | sed 's/^/  - /'
    return 1
  fi

  ok "业务镜像 latest 校验通过"
}

smoke_verify_case_api() {
  local status_code

  if ! service_selected "apiGateway" && ! service_selected "caseService"; then
    info "本次未发布 apiGateway/caseService，跳过工单接口冒烟"
    return 0
  fi

  info "执行工单接口冒烟验证"
  status_code="$($KUBECTL exec -n hci-troubleshoot deploy/api-gateway -- \
    python -c "import httpx; r=httpx.get('http://case-service:8001/api/cases/?client_id=release-smoke', timeout=10); print(r.status_code)" \
    2>/dev/null | tr -d '[:space:]')"

  if [[ "$status_code" != "200" ]]; then
    error "工单接口冒烟失败: GET /api/cases 返回 ${status_code:-UNKNOWN}"
    return 1
  fi

  ok "工单接口冒烟通过: GET /api/cases -> 200"
}

smoke_verify_ai_reply() {
  local smoke_output

  if ! service_selected "apiGateway" \
    && ! service_selected "caseService" \
    && ! service_selected "conversationService" \
    && ! service_selected "schedulerService" \
    && ! service_selected "openclaw"; then
    info "本次未发布 AI 相关服务，跳过 AI 回复冒烟"
    return 0
  fi

  info "执行 AI 回复冒烟验证（创建工单→创建会话→发送消息）"
  if ! smoke_output="$($KUBECTL exec -i -n hci-troubleshoot deploy/api-gateway -- python - <<'PY'
import json
import uuid

import httpx

base = "http://127.0.0.1:8000/api"
client_id = f"release-ai-smoke-{uuid.uuid4().hex[:8]}"
timeout = httpx.Timeout(connect=10.0, read=90.0, write=10.0, pool=10.0)


def ensure_status(resp: httpx.Response, expected: set[int], step: str) -> None:
    if resp.status_code not in expected:
        raise RuntimeError(f"{step} 失败: status={resp.status_code}, body={resp.text[:300]}")


with httpx.Client(base_url=base, headers={"X-Client-ID": client_id}, timeout=timeout) as client:
    create_resp = client.post(
        "/cases/",
        json={
            "client_id": client_id,
            "title": "发布 AI 冒烟",
            "description": "自动校验 AI 回复非空",
            "assistant_type": "openclaw",
        },
    )
    ensure_status(create_resp, {200, 201}, "创建工单")
    case_id = create_resp.json()["case_id"]

    confirm_resp = client.put(f"/cases/{case_id}/confirm")
    ensure_status(confirm_resp, {200}, "确认工单")

    conv_resp = client.post(f"/conversations/?case_id={case_id}&assistant_type=openclaw")
    ensure_status(conv_resp, {200, 201}, "创建会话")
    conversation_id = conv_resp.json()["conversation_id"]

    first_token = ""
    with client.stream(
        "POST",
        f"/conversations/{conversation_id}/message",
        headers={"Content-Type": "application/json"},
        json={
            "case_id": case_id,
            "role": "user",
            "content": "请仅回复：链路检查通过",
        },
    ) as stream_resp:
        ensure_status(stream_resp, {200}, "发送消息")

        for raw_line in stream_resp.iter_lines():
            if not raw_line:
                continue

            line = raw_line.decode("utf-8") if isinstance(raw_line, bytes) else raw_line
            if not line.startswith("data: "):
                continue

            data = line[6:].strip()
            if data == "[DONE]":
                break

            try:
                payload = json.loads(data)
                token = payload.get("content") or ""
            except json.JSONDecodeError:
                token = data

            if token:
                first_token = token
                break

    if not first_token:
        raise RuntimeError("AI 返回为空（未读取到 token）")

    print(f"AI_SMOKE_OK case_id={case_id} conversation_id={conversation_id} token={first_token[:40]}")
PY
)"; then
    error "AI 回复冒烟失败"
    if [[ -n "${smoke_output:-}" ]]; then
      echo "$smoke_output"
    fi
    return 1
  fi

  ok "AI 回复冒烟通过: ${smoke_output}"
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
  warn_scheduler_release_dependency

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
    run_db_migrations
    validate_db_schema_contract
    deploy_to_k3s "$OVERRIDE_FILE"
    verify_image_consistency
    verify_no_latest_business_images
    smoke_verify_case_api
    smoke_verify_ai_reply
  else
    warn "已跳过部署 (--skip-deploy)"
    warn "已跳过镜像一致性校验（未部署无法校验 Deployment 镜像）"
  fi

  if [[ "$SKIP_VERIFY" == false ]]; then
    run_post_verify
  else
    warn "已跳过综合验证 (--skip-verify)"
  fi

  print_release_summary
}

main "$@"
