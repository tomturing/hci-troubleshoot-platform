#!/usr/bin/env bash
# =============================================================================
# 🔴 运维脚本 — 无 GitHub Actions 本地构建部署（CI 替代方案）
# =============================================================================
# 职责：在 GitHub Actions 不可用时，本地完整替代 CI 构建 → 部署流程
#
# 两种模式（通过第一个参数选择）：
#   push   — 构建镜像 → push ghcr.io → 更新 env repo → ArgoCD 自动同步（推荐）
#   import — 构建镜像 → 直接导入 K3s containerd → 更新 env repo → ArgoCD 同步
#
# 环境变量：
#   SERVICES   — 逗号分隔的服务名，默认全量（如 kb-service 或 api-gateway,kb-service）
#   IMAGE_TAG  — 自定义 tag，默认生成 CI 同款格式（如 20260411-1430-abc1234）
#   DRY_RUN    — 设为 1 只打印命令不执行
#
# 使用示例：
#   # 仅部署 kb-service（push 模式，推荐）
#   SERVICES=kb-service bash scripts/ops/local-deploy.sh push
#
#   # 全量构建 push
#   bash scripts/ops/local-deploy.sh push
#
#   # 离线模式：直接导入 K3s（ghcr.io 不可用时）
#   SERVICES=kb-service bash scripts/ops/local-deploy.sh import
#
#   # 预览模式（不执行任何操作）
#   DRY_RUN=1 SERVICES=kb-service bash scripts/ops/local-deploy.sh push
#
# 影响范围：🔴 生产部署链路（构建 + 推送/导入 + 更新 env repo + ArgoCD 同步）
#           确认服务可用后再操作
# =============================================================================
set -euo pipefail

# ── 路径常量 ─────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
ENV_REPO_PATH="${ENV_REPO_PATH:-/mnt/d/aihci/hci-platform-env}"
TARGET_ENV="${TARGET_ENV:-dev}"

# ── 镜像仓库配置（与 CI 完全一致）────────────────────────────────────────────
REGISTRY="ghcr.io/tomturing/hci-troubleshoot-platform"

# ── 入参 ─────────────────────────────────────────────────────────────────────
MODE="${1:-push}"   # push | import
if [[ "$MODE" != "push" && "$MODE" != "import" ]]; then
  echo "用法: bash $0 [push|import]"
  echo "  push   — 构建并推送到 ghcr.io，ArgoCD 自动同步（推荐）"
  echo "  import — 构建并直接导入 K3s containerd（ghcr.io 不可用时）"
  exit 1
fi

# ── 颜色 ─────────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info()  { echo -e "${BLUE}[INFO]${NC}  $*"; }
ok()    { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; }

# ── DRY RUN ──────────────────────────────────────────────────────────────────
DRY_RUN="${DRY_RUN:-0}"
run() {
  if [[ "$DRY_RUN" == "1" ]]; then
    echo -e "${YELLOW}[DRY]${NC}   $*"
  else
    eval "$@"
  fi
}

# ── 生成 Image Tag（与 CI 同款格式 YYYYMMDD-HHMM-<sha7>）─────────────────────
GIT_SHA7="$(git -C "$PROJECT_ROOT" rev-parse --short=7 HEAD)"
IMAGE_TAG="${IMAGE_TAG:-$(date -u +%Y%m%d-%H%M)-${GIT_SHA7}}"

info "============================================================"
info "  本地部署脚本 — CI 替代方案"
info "  模式:    ${MODE}"
info "  Tag:     ${IMAGE_TAG}"
info "  环境:    ${TARGET_ENV}"
info "  环境仓:  ${ENV_REPO_PATH}"
info "  DRY RUN: ${DRY_RUN}"
info "============================================================"
echo ""

# ── 服务定义（service名 → Helm key / context / dockerfile）──────────────────
# 格式: "service名:helm_key:build_context:dockerfile"
ALL_SERVICES=(
  "api-gateway:apiGateway:backend:api-gateway/Dockerfile"
  "case-service:caseService:backend:case-service/Dockerfile"
  "conversation-service:conversationService:backend:conversation-service/Dockerfile"
  "scheduler-service:schedulerService:backend:scheduler-service/Dockerfile"
  "kb-service:kbService:backend:kb-service/Dockerfile"
  "customer-ui:customerUI:frontend:customer/Dockerfile"
  "admin-ui:adminUI:frontend:admin/Dockerfile"
)

# ── 解析 SERVICES 过滤器 ─────────────────────────────────────────────────────
SERVICES_FILTER="${SERVICES:-}"
declare -A SERVICES_MAP
if [[ -n "$SERVICES_FILTER" ]]; then
  IFS=',' read -r -a _svcs <<< "$SERVICES_FILTER"
  for s in "${_svcs[@]}"; do
    SERVICES_MAP["$(echo "$s" | xargs)"]=1
  done
fi

WORK_SERVICES=()
for entry in "${ALL_SERVICES[@]}"; do
  svc="$(echo "$entry" | cut -d: -f1)"
  if [[ -z "$SERVICES_FILTER" ]] || [[ -n "${SERVICES_MAP[$svc]:-}" ]]; then
    WORK_SERVICES+=("$entry")
  fi
done

if [[ "${#WORK_SERVICES[@]}" -eq 0 ]]; then
  error "SERVICES=${SERVICES_FILTER} 没有匹配到任何服务"
  error "合法服务名: api-gateway, case-service, conversation-service, scheduler-service, kb-service, customer-ui, admin-ui"
  exit 1
fi

info "待处理服务（共 ${#WORK_SERVICES[@]} 个）:"
for entry in "${WORK_SERVICES[@]}"; do
  svc="$(echo "$entry" | cut -d: -f1)"
  echo "  - ${svc}"
done
echo ""

# ============================================================================
# PHASE 1: 构建镜像
# ============================================================================
info "── Phase 1: 构建 Docker 镜像 ──────────────────────────────────────────"
echo ""

# 构建前同步 pnpm lockfile（PIT-028：避免 frozen-lockfile 报错）
if echo "${WORK_SERVICES[@]}" | grep -qE "customer-ui|admin-ui"; then
  info "检测到前端服务，同步 pnpm lockfile..."
  if command -v pnpm &>/dev/null; then
    run "(cd '${PROJECT_ROOT}/frontend' && pnpm install --lockfile-only --ignore-scripts 2>&1 | tail -3)"
    ok "pnpm lockfile 已同步"
  else
    warn "pnpm 未安装，跳过 lockfile 同步（可能导致前端构建失败）"
  fi
  echo ""
fi

BUILT=0; FAILED=0
declare -a BUILT_IMAGES=()

for entry in "${WORK_SERVICES[@]}"; do
  svc="$(echo "$entry" | cut -d: -f1)"
  ctx_dir="$(echo "$entry" | cut -d: -f3)"
  dockerfile="$(echo "$entry" | cut -d: -f4)"
  full_image="${REGISTRY}/${svc}:${IMAGE_TAG}"

  info "[$((BUILT+FAILED+1))/${#WORK_SERVICES[@]}] 构建 ${full_image} ..."

  # --network host：Clash TUN 场景容器内需走宿主机网络（PIT-028）
  if run "docker build --network host \
      -t '${full_image}' \
      -t '${REGISTRY}/${svc}:latest' \
      -f '${PROJECT_ROOT}/${ctx_dir}/${dockerfile}' \
      '${PROJECT_ROOT}/${ctx_dir}' --quiet"; then
    ok "  → ${svc} 构建成功"
    BUILT=$((BUILT + 1))
    BUILT_IMAGES+=("$entry")
  else
    error "  → ${svc} 构建失败"
    FAILED=$((FAILED + 1))
  fi
done

echo ""
info "Phase 1 完成: ${GREEN}${BUILT} 成功${NC} / ${RED}${FAILED} 失败${NC}"
if [[ $FAILED -gt 0 ]]; then
  error "存在构建失败，终止部署"
  exit 1
fi
echo ""

# ============================================================================
# PHASE 2: 推送或导入
# ============================================================================
if [[ "$MODE" == "push" ]]; then
  info "── Phase 2: 推送镜像到 ghcr.io ────────────────────────────────────────"
  echo ""

  # 检查 ghcr.io 登录状态（通过 ~/.docker/config.json 判断）
  if ! grep -q "ghcr.io" "${HOME}/.docker/config.json" 2>/dev/null; then
    warn "未检测到 ghcr.io 登录状态，尝试自动登录..."
    if [[ "$DRY_RUN" != "1" ]]; then
      # 优先使用 gh CLI token（已通过 gh auth login，无需额外 PAT）
      if command -v gh &>/dev/null && gh auth token &>/dev/null; then
        info "使用 gh CLI token 登录 ghcr.io..."
        gh auth token | docker login ghcr.io -u "$(gh api user --jq .login 2>/dev/null || echo tomturing)" --password-stdin
      else
        error "gh CLI 未登录，且未找到 ghcr.io 凭证。请先执行："
        error "  gh auth login  或  echo \$GITHUB_TOKEN | docker login ghcr.io -u <用户名> --password-stdin"
        error "如 ghcr.io 不可用，改用 import 模式：bash $0 import"
        exit 1
      fi
    fi
  fi

  PUSHED=0; PUSH_FAILED=0
  for entry in "${BUILT_IMAGES[@]}"; do
    svc="$(echo "$entry" | cut -d: -f1)"
    full_image="${REGISTRY}/${svc}:${IMAGE_TAG}"

    info "推送 ${full_image} ..."
    if run "docker push '${full_image}' && docker push '${REGISTRY}/${svc}:latest'"; then
      ok "  → ${svc} 推送成功"
      PUSHED=$((PUSHED + 1))
    else
      error "  → ${svc} 推送失败"
      PUSH_FAILED=$((PUSH_FAILED + 1))
    fi
  done

  echo ""
  info "Phase 2 完成: ${GREEN}${PUSHED} 成功${NC} / ${RED}${PUSH_FAILED} 失败${NC}"
  if [[ $PUSH_FAILED -gt 0 ]]; then
    error "存在推送失败，终止部署"
    exit 1
  fi

else  # import 模式
  info "── Phase 2: 导入镜像到 K3s containerd ─────────────────────────────────"
  echo ""

  K3S_CTR="sudo -n k3s ctr"
  if ! ${K3S_CTR} version &>/dev/null 2>&1; then
    K3S_CTR="k3s ctr"
    if ! ${K3S_CTR} version &>/dev/null 2>&1; then
      error "无法访问 k3s ctr，请确认 K3s 正在运行且有权限"
      error "尝试: sudo -v 然后重跑"
      exit 1
    fi
  fi

  IMPORTED=0; IMPORT_FAILED=0
  for entry in "${BUILT_IMAGES[@]}"; do
    svc="$(echo "$entry" | cut -d: -f1)"
    full_image="${REGISTRY}/${svc}:${IMAGE_TAG}"

    info "导入 ${full_image} → K3s containerd ..."
    if run "docker save '${full_image}' | ${K3S_CTR} images import -"; then
      # 导入后验证
      if [[ "$DRY_RUN" != "1" ]] && ! ${K3S_CTR} images ls | grep -q "${full_image}"; then
        error "  → ${svc} 导入命令成功，但 containerd 中未找到镜像！"
        IMPORT_FAILED=$((IMPORT_FAILED + 1))
        continue
      fi
      ok "  → ${svc} 导入成功 ✓"
      IMPORTED=$((IMPORTED + 1))
    else
      error "  → ${svc} 导入失败"
      IMPORT_FAILED=$((IMPORT_FAILED + 1))
    fi
  done

  echo ""
  info "Phase 2 完成: ${GREEN}${IMPORTED} 成功${NC} / ${RED}${IMPORT_FAILED} 失败${NC}"
  if [[ $IMPORT_FAILED -gt 0 ]]; then
    error "存在导入失败，终止部署"
    exit 1
  fi
fi

echo ""

# ============================================================================
# PHASE 3: 更新 env repo（hci-platform-env）中的 image tag
# ============================================================================
info "── Phase 3: 更新 env repo 镜像标签 ────────────────────────────────────"
echo ""

if [[ ! -d "$ENV_REPO_PATH" ]]; then
  error "env repo 不存在: ${ENV_REPO_PATH}"
  error "请先 clone: git clone https://github.com/tomturing/hci-platform-env.git ${ENV_REPO_PATH}"
  exit 1
fi

# 构建 SERVICES_CSV（仅更新本次构建的服务）
HELM_KEYS=()
for entry in "${BUILT_IMAGES[@]}"; do
  helm_key="$(echo "$entry" | cut -d: -f2)"
  HELM_KEYS+=("$helm_key")
done
SERVICES_CSV="$(IFS=','; echo "${HELM_KEYS[*]}")"

info "更新服务: ${SERVICES_CSV}"
info "目标 tag: ${IMAGE_TAG}"

run "ENV_REPO_PATH='${ENV_REPO_PATH}' \
  TARGET_ENV='${TARGET_ENV}' \
  IMAGE_TAG='${IMAGE_TAG}' \
  SERVICES_CSV='${SERVICES_CSV}' \
  bash '${PROJECT_ROOT}/scripts/ops/sync-env-repo-tags.sh'"

ok "env repo values.yaml 已更新"
echo ""

# ── 提交并推送 env repo ───────────────────────────────────────────────────────
info "提交 env repo 变更..."
COMMIT_MSG="chore: 本地构建同步 ${TARGET_ENV} 环境镜像标签为 ${IMAGE_TAG} (services: ${SERVICES_CSV})"

if run "cd '${ENV_REPO_PATH}' && git add environments/${TARGET_ENV}/values.yaml && git diff --cached --quiet"; then
  info "  → 无变更，跳过提交"
else
  run "cd '${ENV_REPO_PATH}' && git commit -m '${COMMIT_MSG}'"
  run "cd '${ENV_REPO_PATH}' && git stash --include-untracked --quiet || true"
  run "cd '${ENV_REPO_PATH}' && git pull --rebase origin main"
  run "cd '${ENV_REPO_PATH}' && git stash pop --quiet || true"
  run "cd '${ENV_REPO_PATH}' && git push origin main"
  ok "env repo 已推送"
fi

echo ""

# ============================================================================
# PHASE 4: 触发 ArgoCD 同步
# ============================================================================
info "── Phase 4: 触发 ArgoCD 同步 ───────────────────────────────────────────"
echo ""

ARGOCD_APP="hci-platform-${TARGET_ENV}"

if command -v argocd &>/dev/null; then
  info "触发 ArgoCD 同步 ${ARGOCD_APP}..."
  run "argocd app sync '${ARGOCD_APP}' --async" || warn "argocd sync 失败（可能未登录），ArgoCD 会在下次轮询时自动同步（约 3 分钟）"
elif kubectl get application "${ARGOCD_APP}" -n argocd &>/dev/null 2>&1; then
  # 用 annotation 触发立即同步（不需要 argocd CLI）
  info "通过 kubectl annotation 触发 ArgoCD 立即同步..."
  run "kubectl annotate application '${ARGOCD_APP}' -n argocd argocd.argoproj.io/refresh=normal --overwrite"
  ok "ArgoCD 同步已触发（约 30 秒生效）"
else
  warn "未找到 ArgoCD 应用 ${ARGOCD_APP}，跳过同步触发"
  warn "ArgoCD 会在下次自动轮询时同步（自动同步已开启，约 3 分钟）"
fi

echo ""
info "============================================================"
ok "🎉 本地部署完成！"
info "  镜像 Tag:  ${IMAGE_TAG}"
info "  部署模式:  ${MODE}"
info "  服务列表:  ${SERVICES_CSV}"
if [[ "$MODE" == "push" ]]; then
  info "  镜像地址:  ${REGISTRY}/<service>:${IMAGE_TAG}"
fi
info ""
info "  验证命令:"
info "    kubectl get pods -n hci-${TARGET_ENV}   # 查看 Pod 状态"
info "    kubectl logs -n hci-${TARGET_ENV} deploy/kb-service --tail 20  # 查看日志"
info "    kubectl get application hci-platform-${TARGET_ENV} -n argocd   # ArgoCD 状态"
info "============================================================"
