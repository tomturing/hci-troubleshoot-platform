#!/usr/bin/env bash
# =============================================================================
# 🟡 运维脚本 — Docker 镜像构建并导入 K3s
# =============================================================================
# 职责：构建 Project 下各服务 Docker 镜像，并导入到 K3s containerd
# 使用场景：应急发布（本地构建）；正常发布不应执行此脚本（走 CI 构建）
# 使用方法：
#   bash scripts/ops/k3s-build.sh              # 构建并导入
#   bash scripts/ops/k3s-build.sh --no-import  # 仅构建，不导入 K3s
# 影响范围：🟡 本地镜像和 K3s containerd（不变更集群运行状态）
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
OPENCLAW_REPO_DIR="${OPENCLAW_REPO_DIR:-${PROJECT_ROOT}/../openclaw}"
IMAGE_TAG="${IMAGE_TAG:-latest}"

default_k3s_ctr_cmd() {
  if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
    echo "k3s ctr"
  else
    echo "sudo -n k3s ctr"
  fi
}

K3S_CTR="${K3S_CTR:-$(default_k3s_ctr_cmd)}"

default_k3s_kubectl_cmd() {
  if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
    echo "k3s kubectl"
  else
    echo "sudo -n k3s kubectl"
  fi
}

K3S_KUBECTL="${K3S_KUBECTL:-$(default_k3s_kubectl_cmd)}"

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
  local tmp_err runtime
  tmp_err="$(mktemp)"

  if ${K3S_CTR} images ls > /dev/null 2>"$tmp_err"; then
    rm -f "$tmp_err"
    return 0
  fi

  runtime="$(${K3S_KUBECTL} get nodes -o jsonpath='{.items[0].status.nodeInfo.containerRuntimeVersion}' 2>/dev/null || true)"
  if [[ "$runtime" == docker://* ]] && grep -q 'cannot access socket /run/k3s/containerd/containerd.sock' "$tmp_err"; then
    warn "检测到节点运行时为 ${runtime}，且未启用内置 containerd socket"
    warn "当前环境无需执行 k3s ctr 导入，将跳过导入步骤"
    rm -f "$tmp_err"
    return 2
  fi

  error "无法访问 K3s containerd"
  error "当前命令: ${K3S_CTR}"
  if [[ -s "$tmp_err" ]]; then
    error "原始错误输出:"
    sed -n '1,8p' "$tmp_err" | while IFS= read -r line; do
      error "  ${line}"
    done
  fi
  rm -f "$tmp_err"

  if [[ "$K3S_CTR" == sudo* ]]; then
    error "请先执行: sudo -v"
    error "或显式指定无需 sudo 的命令，例如: K3S_CTR='k3s ctr' bash scripts/k3s-build.sh"
  else
    error "请检查 k3s 服务和 containerd 是否正常（例如: sudo k3s ctr version）"
  fi
  return 1
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
  "hci-kb-service             ${PROJECT_ROOT}/backend       kb-service/Dockerfile"
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
  # 仅同步 lockfile，避免 pnpm 因重建 node_modules 弹出交互确认导致脚本看似卡住。
  (cd "${PROJECT_ROOT}/frontend" && pnpm install --lockfile-only --ignore-scripts 2>&1 | tail -3) \
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
  local_ctr_access_status=0
  echo ""
  info "开始导入镜像到 K3s containerd..."

  ensure_k3s_ctr_access || local_ctr_access_status=$?
  if [[ "$local_ctr_access_status" -eq 2 ]]; then
    warn "已跳过镜像导入（Docker runtime 环境）"
    echo ""
    ok "全部完成! ✅"
    exit 0
  fi

  if [[ "$local_ctr_access_status" -ne 0 ]]; then
    exit 1
  fi

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
