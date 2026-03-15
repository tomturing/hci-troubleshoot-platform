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
OPENCLAW_REPO_DIR="${OPENCLAW_REPO_DIR:-${PROJECT_ROOT}/../openclaw}"
IMAGE_TAG="${IMAGE_TAG:-latest}"
K3S_CTR="${K3S_CTR:-sudo -n k3s ctr}"

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

ensure_k3s_ctr_access() {
  if ${K3S_CTR} images ls >/dev/null 2>&1; then
    return 0
  fi

  error "无法以非交互方式访问 K3s containerd"
  error "当前命令: ${K3S_CTR}"
  error "请先执行: sudo -v"
  error "或显式指定无需 sudo 的命令，例如: K3S_CTR='k3s ctr' bash scripts/k3s-build.sh"
  exit 1
}

IMPORT_K3S=true
[[ "${1:-}" == "--no-import" ]] && IMPORT_K3S=false
BUILD_ONLY_IMAGES="${BUILD_ONLY_IMAGES:-}"

declare -A BUILD_ONLY_MAP

if [[ -n "$BUILD_ONLY_IMAGES" ]]; then
  IFS=',' read -r -a _raw_images <<< "$BUILD_ONLY_IMAGES"
  for _img in "${_raw_images[@]}"; do
    _cleaned="$(echo "$_img" | xargs)"
    [[ -n "$_cleaned" ]] && BUILD_ONLY_MAP["$_cleaned"]=1
  done
fi

should_build_image() {
  local image_name="$1"
  if [[ -z "$BUILD_ONLY_IMAGES" ]]; then
    return 0
  fi
  [[ -n "${BUILD_ONLY_MAP[$image_name]:-}" ]]
}

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

if [[ -f "${OPENCLAW_REPO_DIR}/Dockerfile" ]]; then
  IMAGES+=("hci-openclaw               ${OPENCLAW_REPO_DIR}       Dockerfile")
else
  warn "未找到 OpenClaw 仓库 Dockerfile: ${OPENCLAW_REPO_DIR}/Dockerfile"
  warn "将跳过 hci-openclaw 镜像构建；若 Helm 启用 openclaw，部署会因缺镜像失败"
fi

WORK_IMAGES=()
for entry in "${IMAGES[@]}"; do
  read -r name _ _ <<< "$entry"
  if should_build_image "$name"; then
    WORK_IMAGES+=("$entry")
  fi
done

if [[ "${#WORK_IMAGES[@]}" -eq 0 ]]; then
  error "没有匹配到可构建镜像，请检查 BUILD_ONLY_IMAGES=${BUILD_ONLY_IMAGES}"
  exit 1
fi

# ============================================================================
# 构建前：同步前端 pnpm lockfile（防止 frozen-lockfile 在容器内报错）
# PIT-028: docker build 必须加 --network host（Clash TUN 环境容器内网络不通）
# ============================================================================
info "同步前端 pnpm lockfile..."
if command -v pnpm &>/dev/null; then
  (cd "${PROJECT_ROOT}/frontend" && pnpm install --ignore-scripts 2>&1 | tail -3) \
    && ok "  → pnpm lockfile 已同步" \
    || warn "  → pnpm lockfile 同步失败（将使用 --no-frozen-lockfile 构建）"
else
  warn "  → pnpm 未安装，跳过 lockfile 同步"
fi
echo ""

# ============================================================================
# 构建所有镜像
# ============================================================================
info "开始构建 ${#WORK_IMAGES[@]} 个 Docker 镜像 (tag=${IMAGE_TAG})..."
echo ""

BUILT=0
FAILED=0

for entry in "${WORK_IMAGES[@]}"; do
  read -r name context dockerfile <<< "$entry"
  info "[$((BUILT+FAILED+1))/${#WORK_IMAGES[@]}] 构建 ${name}:${IMAGE_TAG} ..."
  
  # --network host：让构建容器走宿主机网络（Clash TUN 场景必须，PIT-028）
  if docker build --network host -t "${name}:${IMAGE_TAG}" -f "${context}/${dockerfile}" "${context}" --quiet; then
    ok "  → ${name}:${IMAGE_TAG} 构建成功"
    BUILT=$((BUILT + 1))
  else
    error "  → ${name}:${IMAGE_TAG} 构建失败!"
    FAILED=$((FAILED + 1))
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

  ensure_k3s_ctr_access

  IMPORTED=0
  for entry in "${WORK_IMAGES[@]}"; do
    read -r name _ _ <<< "$entry"
    info "  导入 ${name}:${IMAGE_TAG} ..."
    if docker save "${name}:${IMAGE_TAG}" | ${K3S_CTR} images import -; then
      ok "  → ${name}:${IMAGE_TAG} 已导入"
      IMPORTED=$((IMPORTED + 1))
    else
      error "  → ${name}:${IMAGE_TAG} 导入失败"
    fi
  done
  
  echo ""
  info "导入完成: ${IMPORTED}/${#WORK_IMAGES[@]}"
  
  echo ""
  info "验证 K3s 中的 HCI 镜像:"
  ${K3S_CTR} images ls | grep "hci-" | awk '{print "  " $1}' || true
fi

echo ""
ok "全部完成! ✅"
