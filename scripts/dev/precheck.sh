#!/usr/bin/env bash
# =============================================================================
# 🟢 开发脚本 — 本地 K3s 环境预检 (precheck.sh)
# =============================================================================
# 在首次本地部署前运行，检测所有已知环境问题并提供修复指引。
# 覆盖已知问题：DEPLOY-001/003/007 / DUAL-001/003/005/006/009
#
# 使用方法：
#   bash scripts/dev/precheck.sh              # 交互式检查（推荐首次使用）
#   bash scripts/dev/precheck.sh --quiet      # 静默模式（仅输出 FAIL，适合 CI）
#   bash scripts/dev/precheck.sh --fix        # 自动修复可安全自动处理的项目
#   bash scripts/dev/precheck.sh --env-repo /path/to/hci-platform-env  # 指定 env 仓库路径
#   bash scripts/dev/precheck.sh --env staging  # 指定部署环境（默认 dev）
#
# 退出码：
#   0 = 全部通过或仅有警告
#   1 = 存在阻断性 FAIL 项目
#
# 参考：docs/21_本地K3s部署日志.md — 问题汇总表
# =============================================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

# ---- 默认参数 ----
ENV_REPO_DIR="${ENV_REPO_DIR:-${PROJECT_ROOT}/../hci-platform-env}"
DEPLOY_ENV="${DEPLOY_ENV:-dev}"
QUIET=false
AUTO_FIX=false

# ---- 解析参数 ----
while [[ $# -gt 0 ]]; do
  case "$1" in
    --quiet|-q)  QUIET=true; shift ;;
    --fix)       AUTO_FIX=true; shift ;;
    --env-repo)  ENV_REPO_DIR="${2:?--env-repo 需要路径参数}"; shift 2 ;;
    --env)       DEPLOY_ENV="${2:?--env 需要 dev|staging|prod}"; shift 2 ;;
    -h|--help)
      grep '^#' "$0" | head -25 | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) echo "未知参数: $1" >&2; exit 1 ;;
  esac
done

# ---- 颜色 ----
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

# ---- 计数器 ----
PASS_COUNT=0; FAIL_COUNT=0; WARN_COUNT=0; SKIP_COUNT=0

# ---- 输出函数 ----
_pass() { PASS_COUNT=$((PASS_COUNT+1)); $QUIET || echo -e "  ${GREEN}✅ PASS${NC}  $*"; }
_fail() { FAIL_COUNT=$((FAIL_COUNT+1)); echo -e "  ${RED}❌ FAIL${NC}  $*"; }
_warn() { WARN_COUNT=$((WARN_COUNT+1)); $QUIET || echo -e "  ${YELLOW}⚠️  WARN${NC}  $*"; }
_info() { $QUIET || echo -e "  ${BLUE}ℹ️  INFO${NC}  $*"; }
_skip() { SKIP_COUNT=$((SKIP_COUNT+1)); $QUIET || echo -e "  ${CYAN}⏭️  SKIP${NC}  $*"; }

# 打印修复建议
_fix_hint() { $QUIET && return; echo -e "     ${BOLD}修复方法:${NC} $*"; }

# 交互式修复（--fix 模式则自动执行）
_ask_fix() {
  local desc="$1"; shift
  local cmd="$*"
  if $QUIET; then return; fi
  echo -e "     ${BOLD}修复方法:${NC} ${cmd}"
  if $AUTO_FIX; then
    echo -e "     ${CYAN}[自动修复]${NC} 正在执行..."
    if eval "$cmd"; then
      echo -e "     ${GREEN}✅ 修复成功${NC}"
    else
      echo -e "     ${RED}❌ 修复失败，请手动执行${NC}"
    fi
    return
  fi
  echo -e "     是否立即自动修复？[y/N] \c"
  read -r answer < /dev/tty
  if [[ "$answer" == [Yy] ]]; then
    eval "$cmd" && echo -e "     ${GREEN}✅ 修复成功${NC}" || echo -e "     ${RED}❌ 修复失败，请手动执行${NC}"
  fi
}

# 节标题
_section() { $QUIET || echo -e "\n${CYAN}── $* ──────────────────────────────────────────${NC}"; }

# 简单 YAML 单层值读取（不依赖 yq）
_yaml_get() {
  grep -E "^\s*${1}\s*:" "$2" 2>/dev/null | head -1 \
    | sed -E 's/.*:[[:space:]]*//' | tr -d '"' | tr -d "'" | tr -d '[:space:]'
}

# ============================================================
# Banner
# ============================================================
$QUIET || cat << 'BANNER'

  ╔══════════════════════════════════════════════════════════╗
  ║   HCI 平台 — 本地 K3s 部署环境预检 (precheck.sh)         ║
  ║   参考：docs/21_本地K3s部署日志.md 避坑汇总               ║
  ╚══════════════════════════════════════════════════════════╝

BANNER

# ============================================================
# 检查组 1：基础系统工具
# ============================================================
_section "1. 基础系统工具"

# 1.1 WSL2 环境检测（本脚本主要面向 WSL2+K3s 场景）
if grep -qi microsoft /proc/version 2>/dev/null; then
  _pass "运行在 WSL2 环境中"
  IN_WSL=true
else
  _warn "当前不在 WSL2 中运行，本脚本部分检查针对 WSL2+K3s 场景，可能有误报"
  IN_WSL=false
fi

# 1.2 sudo 非交互权限
# 部署脚本使用 sudo -n（非交互），若需要密码则静默失败，导致真正权限错误难以排查
if sudo -n true 2>/dev/null; then
  _pass "sudo 非交互权限 (sudo -n) 可用"
else
  _fail "sudo 需要密码，部署脚本使用 sudo -n 会静默失败 [所有部署相关]"
  _fix_hint "方案 A: 执行 'sudo -v' 后立即运行部署（有效期 ~15 分钟）"
  _fix_hint "方案 B: 将用户加入 NOPASSWD sudoers（永久）:"
  _fix_hint "  echo \"\$(whoami) ALL=(ALL) NOPASSWD:ALL\" | sudo tee /etc/sudoers.d/\$(whoami)"
fi

# 1.3 Docker 可用性
if command -v docker &>/dev/null; then
  _pass "Docker 已安装: $(docker --version 2>/dev/null | head -1)"
else
  _fail "Docker 未安装（镜像构建依赖 Docker）"
  _fix_hint "安装 Docker Desktop (Windows+WSL2) 或: sudo apt install docker.io"
fi

# 1.4 Docker BuildX 插件权限（问题 DEPLOY-007）
# 已知：Docker Desktop 安装后 buildx 插件文件可能缺少可执行位，导致构建命令降级但偶发错误
BUILDX_PATHS=(
  "$HOME/.docker/cli-plugins/docker-buildx"
  "/usr/local/lib/docker/cli-plugins/docker-buildx"
  "/usr/lib/docker/cli-plugins/docker-buildx"
)
BUILDX_FOUND=false
for bp in "${BUILDX_PATHS[@]}"; do
  if [[ -f "$bp" ]]; then
    BUILDX_FOUND=true
    if [[ -x "$bp" ]]; then
      _pass "Docker BuildX 插件权限正常: $bp"
    else
      _fail "Docker BuildX 插件缺少执行权限: $bp [DEPLOY-007]"
      _ask_fix "chmod +x 修复执行权限" "chmod +x '$bp'"
    fi
    break
  fi
done
$BUILDX_FOUND || _info "Docker BuildX 插件未找到于常见路径（可能使用系统级安装，无问题）"

# 1.5 K3s 安装检测
K3S_BIN=""
if command -v k3s &>/dev/null; then
  K3S_BIN="k3s"
elif sudo -n which k3s &>/dev/null 2>&1; then
  K3S_BIN="sudo -n k3s"
fi

if [[ -n "$K3S_BIN" ]]; then
  _pass "K3s 二进制可用"
  # 检查 K3s 是否在运行
  if $K3S_BIN kubectl get nodes &>/dev/null 2>&1; then
    _pass "K3s 节点可访问（集群运行中）"
  else
    _fail "K3s 未运行或节点不可达"
    _ask_fix "启动 K3s 服务" "sudo systemctl start k3s && sleep 15"
  fi
else
  _fail "K3s 未安装"
  _fix_hint "curl -sfL https://get.k3s.io | sh -s - --write-kubeconfig-mode 644"
fi

# 1.6 Helm v3
if command -v helm &>/dev/null; then
  HELM_VER=$(helm version --short 2>/dev/null | head -1)
  _pass "Helm 已安装: $HELM_VER"
else
  _fail "Helm 未安装（Helm upgrade 无法执行）"
  _fix_hint "curl https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash"
fi

# ============================================================
# 检查组 2：仓库与配置文件
# ============================================================
_section "2. 仓库与配置文件"

# 2.1 App 仓库结构
if [[ -f "${PROJECT_ROOT}/deploy/helm/hci-platform/values.yaml" ]]; then
  _pass "App 仓库结构正常: ${PROJECT_ROOT}"
else
  _fail "App 仓库结构异常，找不到 deploy/helm/hci-platform/values.yaml"
fi

# 2.2 Env 仓库（双仓模式必须，问题 DUAL-001 的前提）
ENV_VALUES="${ENV_REPO_DIR}/environments/${DEPLOY_ENV}/values.yaml"
if [[ -f "$ENV_VALUES" ]]; then
  _pass "Env 仓库 values 文件存在: $ENV_VALUES"

  # 2.2a ghcrToken（DUAL-001：K3s 需要认证才能拉取 ghcr.io 私有镜像）
  GHCR_TOKEN=$(_yaml_get ghcrToken "$ENV_VALUES")
  IMAGE_REGISTRY=$(_yaml_get imageRegistry "$ENV_VALUES")
  if [[ "$IMAGE_REGISTRY" == *"ghcr.io"* ]]; then
    if [[ -n "$GHCR_TOKEN" && "$GHCR_TOKEN" != "your-github-pat-here" ]]; then
      _pass "env values 包含 ghcrToken（ghcr.io 镜像拉取凭据已设置）"
    else
      _fail "imageRegistry 指向 ghcr.io 但 ghcrToken 未设置 [DUAL-001]"
      _fix_hint "在 ${ENV_VALUES} 中设置 ghcrToken: <GitHub Personal Access Token>"
    fi
  else
    _info "imageRegistry 不使用 ghcr.io，跳过 Token 检查（当前: ${IMAGE_REGISTRY:-未设置}）"
  fi

  # 2.2b postgresPassword 默认值检查（DEPLOY-001：触发 Chart 安全校验）
  PG_PASS=$(_yaml_get postgresPassword "$ENV_VALUES")
  DOMAIN=$(_yaml_get domain "$ENV_VALUES")
  if [[ "$PG_PASS" == "dev_password_123" && -n "$DOMAIN" && "$DOMAIN" != "hci.local" && "$DOMAIN" != "" ]]; then
    _fail "domain 不是 hci.local 但 postgresPassword 仍为默认弱密码，Chart 校验会阻断部署 [DEPLOY-001]"
    _fix_hint "在 ${ENV_VALUES} 中修改 postgresPassword 为强密码"
  elif [[ "$PG_PASS" == "dev_password_123" ]]; then
    _warn "postgresPassword 仍为默认值 'dev_password_123'（本地 dev 可接受，生产禁止）"
  else
    _pass "env values postgresPassword 已设置自定义值"
  fi

  # 2.2c openclawToken（直连智谱 AI 必须）
  OPENCLAW_TOKEN=$(_yaml_get openclawToken "$ENV_VALUES")
  OPENCLAW_BASE_URL=$(_yaml_get openclawBaseUrl "$ENV_VALUES")
  if [[ -n "$OPENCLAW_TOKEN" && "$OPENCLAW_TOKEN" != "hci-dev-openclaw-token" ]]; then
    _pass "env values openclawToken 已设置（AI 后端凭据就绪）"
  else
    _warn "env values openclawToken 未设置或为默认值 — AI 对话功能将不可用 [DUAL-007]"
    _fix_hint "在 ${ENV_VALUES} 中设置 openclawToken: <ZhipuAI API Key>"
  fi
else
  _fail "Env 仓库 values 文件不存在: $ENV_VALUES"
  _fix_hint "克隆 env 仓库: git clone git@github.com:tomturing/hci-platform-env.git ${ENV_REPO_DIR}"
fi

# 2.3 .local/ 覆盖文件（可选，优先级最高）
LOCAL_OVERRIDE="${PROJECT_ROOT}/.local/values-dualrepo-local.yaml"
if [[ -f "$LOCAL_OVERRIDE" ]]; then
  _pass ".local/values-dualrepo-local.yaml 存在（本地持久化覆盖已配置）"
else
  _info ".local/values-dualrepo-local.yaml 不存在（会自动生成临时覆盖，属正常情况）"
fi

# 2.4 openclaw 仓库（DEPLOY-003）
# openclaw 未克隆时，部署脚本会自动禁用三个 claw 工作负载（不阻断）
OPENCLAW_REPO="${PROJECT_ROOT}/../openclaw"
if [[ -d "$OPENCLAW_REPO" ]]; then
  _pass "openclaw 仓库已克隆: ${OPENCLAW_REPO}（AI Pod 可启用）"
else
  _warn "openclaw 仓库未克隆 (${OPENCLAW_REPO}) — AI worker Pod 将被自动禁用 [DEPLOY-003]"
  _info "不影响基础功能。若已配置 openclawToken（直连智谱 AI），对话功能仍可用"
fi

# ============================================================
# 检查组 3：网络环境（nip.io / Clash TUN）
# ============================================================
_section "3. 网络环境"

# 3.1 WSL IP 检测
WSL_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || true)
if [[ -n "$WSL_IP" ]]; then
  _pass "WSL IP 检测成功: ${WSL_IP}"
else
  _warn "无法获取 WSL IP（hostname -I 返回空），nip.io 访问地址无法确定"
fi

# 3.2 nip.io DNS 解析 / Clash TUN Fake-IP 检测（DUAL-005）
# Clash TUN 默认 Fake-IP 地址段为 198.18.0.0/15
# nip.io 正常情况下应将 1.2.3.4.nip.io 解析为 1.2.3.4
NIP_IP=$(python3 -c "import socket; print(socket.gethostbyname('1.2.3.4.nip.io'))" 2>/dev/null || true)
if [[ -z "$NIP_IP" ]]; then
  _warn "无法解析 1.2.3.4.nip.io（DNS 不通或 python3 不可用），跳过 Clash 检测"
elif [[ "$NIP_IP" == "1.2.3.4" ]]; then
  _pass "nip.io DNS 解析正常（1.2.3.4.nip.io → 1.2.3.4）"
elif [[ "$NIP_IP" == 198.18.* || "$NIP_IP" == 198.19.* ]]; then
  _fail "检测到 Clash TUN Fake-IP 正在劫持 nip.io DNS（解析到 ${NIP_IP}）[DUAL-005]"
  _info "这会导致浏览器访问 http://${WSL_IP:-<WSL_IP>}.nip.io/ 出现 ERR_EMPTY_RESPONSE"
  _fix_hint "方案 A（推荐）：在 Clash 订阅配置的 prepend-rules 添加："
  _fix_hint "  - DOMAIN-SUFFIX,nip.io,DIRECT"
  _fix_hint "方案 B：在 Windows hosts 文件添加（C:\\Windows\\System32\\drivers\\etc\\hosts）:"
  _fix_hint "  ${WSL_IP:-<WSL_IP>}  ${WSL_IP:-<WSL_IP>}.nip.io"
  _fix_hint "方案 C：部署时加 --no-nipio 参数，通过裸 IP 直接访问（无需 DNS 解析）"
else
  _warn "nip.io 解析到意外 IP: ${NIP_IP}（预期 1.2.3.4）"
  if [[ "$NIP_IP" == "$WSL_IP" ]]; then
    _info "→ 解析到 WSL IP，可能是 /etc/hosts 或 Windows hosts 中有手动条目（正常）"
  else
    _fix_hint "请检查 DNS 设置，或使用 --no-nipio 绕过 nip.io"
  fi
fi

# 3.3 Windows hosts 中是否已有 nip.io 解析条目
WIN_HOSTS="/mnt/c/Windows/System32/drivers/etc/hosts"
if [[ -f "$WIN_HOSTS" ]]; then
  if grep -q "nip.io" "$WIN_HOSTS" 2>/dev/null; then
    _pass "Windows hosts 文件中检测到 nip.io 解析条目"
  else
    _info "Windows hosts 中无 nip.io 条目（如遇 Clash DNS 问题可手动添加）"
  fi
fi

# 3.4 ZhipuAI API 可达性（DUAL-007/008：直连智谱 AI 时必须可访问）
ZAI_STATUS=$(python3 - << 'PYEOF' 2>/dev/null || echo "skip"
import urllib.request, ssl, socket
ctx = ssl.create_default_context()
try:
    urllib.request.urlopen('https://open.bigmodel.cn', timeout=5, context=ctx)
    print("ok")
except socket.timeout:
    print("timeout")
except urllib.error.URLError as e:
    print("fail: " + str(e.reason))
except Exception as e:
    print("fail: " + str(e))
PYEOF
)
case "$ZAI_STATUS" in
  ok)      _pass "ZhipuAI API (open.bigmodel.cn) 可访问" ;;
  timeout) _warn "ZhipuAI API 连接超时（网络延迟高，AI 对话响应可能较慢）[DUAL-007]" ;;
  skip)    _info "python3 不可用，跳过 ZhipuAI 可达性检测" ;;
  *)       _warn "ZhipuAI API 不可达: ${ZAI_STATUS} [DUAL-007]
     → 若使用直连智谱 AI 模式，请确保 open.bigmodel.cn 可访问（检查代理配置）" ;;
esac

# ============================================================
# 检查组 4：K3s 镜像与存储状态
# ============================================================
_section "4. K3s 镜像与存储状态"

K3S_CTR="${K3S_BIN:+sudo -n k3s ctr}"
K3S_KUBECTL="${K3S_BIN:+sudo -n k3s kubectl}"

if [[ -z "$K3S_BIN" ]]; then
  _skip "K3s 未安装，跳过镜像和存储检查"
else
  # 4.1 本地 hci- 镜像数量
  HCI_IMAGE_COUNT=$(sudo -n k3s ctr images list 2>/dev/null | grep -c "hci-" || echo "0")
  if [[ "$HCI_IMAGE_COUNT" -gt 0 ]]; then
    _pass "K3s containerd 已有 ${HCI_IMAGE_COUNT} 个 hci- 镜像"

    # 检查是否同时有 ghcr.io 路径的镜像（双仓模式 env repo 用 ghcr.io 路径时需要）
    GHCR_HCI_COUNT=$(sudo -n k3s ctr images list 2>/dev/null | grep -c "ghcr.io.*hci-" || echo "0")
    if [[ "${GHCR_HCI_COUNT}" -gt 0 ]]; then
      _pass "K3s 中有 ghcr.io 路径的 hci 镜像（${GHCR_HCI_COUNT} 个）[DUAL-001 已处理]"
    else
      _warn "K3s 中无 ghcr.io 路径的镜像，若 imageRegistry=ghcr.io/... 则需重标签 [DUAL-001]"
      _info "部署脚本会配置 ghcr.io 认证（registries.yaml），但需先执行重标签或等待 K3s 拉取"
      _info "重标签命令示例（TAG 为实际镜像 tag）:"
      _info "  for svc in api-gateway case-service ...; do"
      _info "    sudo k3s ctr images tag docker.io/library/hci-\$svc:\$TAG ghcr.io/tomturing/hci-troubleshoot-platform/hci-\$svc:latest"
      _info "  done"
    fi
  else
    _warn "K3s containerd 中无 hci- 镜像，需先构建并导入"
    _fix_hint "运行镜像构建脚本: bash ${PROJECT_ROOT}/scripts/ops/k3s-build.sh"
  fi

  # 4.2 postgres PVC 存在性检测（升级场景告警）
  # 若 PVC 已存在且密码或 schema 发生变化，需要特别注意
  NS="hci-troubleshoot"
  if sudo -n k3s kubectl -n "$NS" get pvc postgres-pvc &>/dev/null 2>&1; then
    _warn "⚠️  检测到 postgres PVC 已存在（升级场景），请注意以下事项 [DUAL-003][DUAL-006][DUAL-009]:"
    _info "  1. 若 postgresPassword 已更改，部署脚本会自动执行 ALTER USER 同步密码"
    _info "  2. 部署完成后脚本会自动运行 SQL 迁移（幂等脚本，可重复执行）"
    _info "  3. 手动执行 helm upgrade 时务必附加 --set dataLayer.manage=true，否则 postgres 会被删除！"
    _info "     正确命令: bash ${PROJECT_ROOT}/scripts/ops/k3s-deploy-dualrepo.sh"
  else
    _pass "未检测到现有 postgres PVC（全新部署环境）"
  fi

  # 4.3 孤立 pool Pod 检测（DEPLOY-004）
  # scheduler-service 动态创建的 pool Pod 不受 Helm 管理，镜像不存在时会持续报错
  ORPHAN_PODS=$(sudo -n k3s kubectl -n "$NS" get pods --no-headers 2>/dev/null \
    | awk '$1 ~ /-pool-/ && ($3=="ErrImagePull" || $3=="ImagePullBackOff" || $3 ~ /^Init:/) {print $1}' \
    | tr '\n' ' ' || true)
  if [[ -n "$ORPHAN_PODS" ]]; then
    _warn "检测到孤立的失败 pool Pod: ${ORPHAN_PODS}[DEPLOY-004]"
    _ask_fix "删除这些孤立 pool Pod" \
      "sudo k3s kubectl -n ${NS} delete pod ${ORPHAN_PODS} --ignore-not-found"
  else
    _pass "无孤立失败的 pool Pod"
  fi
fi

# ============================================================
# 检查组 5：磁盘空间
# ============================================================
_section "5. 磁盘空间"

DISK_AVAIL_GB=$(df -BG "${PROJECT_ROOT}" 2>/dev/null | awk 'NR==2 {gsub("G","",$4); print $4}' || echo "0")
if [[ "${DISK_AVAIL_GB:-0}" -ge 10 ]]; then
  _pass "磁盘空间充足: ${DISK_AVAIL_GB}GB 可用"
elif [[ "${DISK_AVAIL_GB:-0}" -ge 5 ]]; then
  _warn "磁盘空间偏低: ${DISK_AVAIL_GB}GB 可用（推荐 ≥10GB，镜像构建约需 3-5GB）"
else
  _fail "磁盘空间严重不足: ${DISK_AVAIL_GB}GB 可用（镜像构建最少需要 5GB）"
  _fix_hint "清理 Docker/K3s 镜像: docker image prune -a 或 sudo k3s ctr images rm <image>"
fi

# ============================================================
# 汇总
# ============================================================
$QUIET || echo ""
echo -e "${CYAN}══════════════════════════════════════════════════════${NC}"
echo -e "  预检结果汇总"
echo -e "${CYAN}══════════════════════════════════════════════════════${NC}"
echo -e "  ${GREEN}✅ PASS: ${PASS_COUNT}${NC}   ${RED}❌ FAIL: ${FAIL_COUNT}${NC}   ${YELLOW}⚠️  WARN: ${WARN_COUNT}${NC}   ${CYAN}⏭️  SKIP: ${SKIP_COUNT}${NC}"
echo ""

if [[ "$FAIL_COUNT" -gt 0 ]]; then
  echo -e "  ${RED}[阻断] 存在 ${FAIL_COUNT} 个需要修复才能部署的问题，请逐项处理后重新运行本脚本${NC}"
  exit 1
elif [[ "$WARN_COUNT" -gt 0 ]]; then
  echo -e "  ${YELLOW}[建议] 存在 ${WARN_COUNT} 个警告项（不阻断部署，建议关注）${NC}"
  $QUIET || echo -e "  ${GREEN}可继续执行: bash ${PROJECT_ROOT}/scripts/ops/k3s-deploy-dualrepo.sh${NC}"
  exit 0
else
  echo -e "  ${GREEN}[通过] 环境检查全部通过！${NC}"
  echo -e "  ${GREEN}运行: bash ${PROJECT_ROOT}/scripts/ops/k3s-deploy-dualrepo.sh${NC}"
  exit 0
fi
