#!/usr/bin/env bash
# =============================================================================
# 🟡 运维脚本 — 双仓模式本地 K3s 部署
# =============================================================================
# 职责：以 GitOps 双仓模式在本地 WSL Ubuntu K3s 中完整部署 HCI 平台
#       App 仓库  : hci-troubleshoot-platform（本仓库，提供 Helm Chart）
#       Env 仓库  : hci-platform-env（提供环境差异 values，需提前 clone 到本地）
#
# 部署顺序：
#   1. hci-platform-infra  — StorageClass + RBAC（集群级）
#   2. hci-platform        — Secret / ConfigMap / 业务服务（从 env 仓库读取 secrets）
#   3. hci-platform-data   — PostgreSQL + Redis（依赖 step 2 创建的 hci-secrets）
#
# 使用方法：
#   # 基本用法（env 仓库在 ../hci-platform-env）
#   bash scripts/ops/k3s-deploy-dualrepo.sh
#
#   # 显式指定 env 仓库路径
#   bash scripts/ops/k3s-deploy-dualrepo.sh --env-repo /path/to/hci-platform-env
#
#   # 指定部署环境（默认 dev）
#   bash scripts/ops/k3s-deploy-dualrepo.sh --env dev
#
#   # 仅安装基础设施和数据层（跳过业务服务）
#   bash scripts/ops/k3s-deploy-dualrepo.sh --infra-only
#
#   # 跳过 Traefik 端口配置
#   bash scripts/ops/k3s-deploy-dualrepo.sh --skip-traefik
#
#   # 使用空 publicUrl（Ingress 匹配任意 host，通过裸 IP 访问，无需 nip.io DNS）
#   bash scripts/ops/k3s-deploy-dualrepo.sh --no-nipio
#
# 依赖：docker / k3s / helm / kubectl
# 回滚: helm -n hci-troubleshoot rollback hci-platform <REVISION>
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

# ============================================================================
# 默认参数
# ============================================================================
ENV_REPO_DIR="${ENV_REPO_DIR:-${PROJECT_ROOT}/../hci-platform-env}"
DEPLOY_ENV="dev"
NAMESPACE="hci-troubleshoot"
INFRA_ONLY=false
SKIP_TRAEFIK=false
NO_NIPIO=false
TRAEFIK_HTTP_PORT=80     # K3s Traefik 对外监听端口（默认80；自定义端口需 HelmChartConfig）
FORCE_DATALAYER_MANAGE=""  # 强制 dataLayer.manage 值；空 = 自动检测

# ============================================================================
# 解析命令行参数
# ============================================================================
while [[ $# -gt 0 ]]; do
  case "$1" in
    --env-repo)
      ENV_REPO_DIR="${2:?--env-repo 需要路径参数}"; shift 2 ;;
    --env)
      DEPLOY_ENV="${2:?--env 需要 dev 或 staging}"; shift 2 ;;
    --namespace)
      NAMESPACE="${2:?--namespace 需要参数}"; shift 2 ;;
    --traefik-port)
      TRAEFIK_HTTP_PORT="${2:?--traefik-port 需要端口号}"; shift 2 ;;
    --traefik-port)
      TRAEFIK_HTTP_PORT="${2:?--traefik-port 需要端口号}"; shift 2 ;;
    --infra-only)
      INFRA_ONLY=true; shift ;;
    --skip-traefik)
      SKIP_TRAEFIK=true; shift ;;
    --no-nipio)
      NO_NIPIO=true; shift ;;
    --datalayer-manage)
      FORCE_DATALAYER_MANAGE="${2:?--datalayer-manage 需要 true 或 false}"; shift 2 ;;
    -h|--help)
      grep '^#' "$0" | sed 's/^# \{0,1\}//' | head -40
      exit 0 ;;
    *)
      echo "未知参数: $1" >&2; exit 1 ;;
  esac
done

# ============================================================================
# 颜色 & 日志函数
# ============================================================================
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; NC='\033[0m'
info()    { echo -e "${BLUE}[INFO]${NC}  $*"; }
ok()      { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*"; }
section() { echo -e "\n${CYAN}═══════════════════════════════════════════════════${NC}"; \
            echo -e "${CYAN}  $*${NC}"; \
            echo -e "${CYAN}═══════════════════════════════════════════════════${NC}"; }

default_kubectl_cmd() {
  [[ "${EUID:-$(id -u)}" -eq 0 ]] && echo "k3s kubectl" || echo "sudo -n k3s kubectl"
}
default_k3s_ctr_cmd() {
  [[ "${EUID:-$(id -u)}" -eq 0 ]] && echo "k3s ctr" || echo "sudo -n k3s ctr"
}

KUBECTL="${KUBECTL:-$(default_kubectl_cmd)}"
K3S_CTR="${K3S_CTR:-$(default_k3s_ctr_cmd)}"
HELM_KUBECONFIG="${KUBECONFIG:-/etc/rancher/k3s/k3s.yaml}"

# ============================================================================
# 步骤 0：前置检查
# ============================================================================
section "步骤 0：前置检查"

# 检查 sudo 非交互权限
if echo "$KUBECTL" | grep -q sudo; then
  if ! sudo -n true 2>/dev/null; then
    error "需要非交互 sudo 权限，请先执行: sudo -v"
    error "或以 root 执行，或设置 KUBECTL='k3s kubectl'"
    exit 1
  fi
  ok "sudo -n 权限已就绪"
fi

# 检查工具
for cmd in helm; do
  if ! command -v "$cmd" &>/dev/null; then
    error "缺少工具: $cmd"
    exit 1
  fi
done
ok "依赖工具检查通过"

# 检查 K3s 是否运行
if ! ${KUBECTL} version --client >/dev/null 2>&1; then
  error "无法连接 K3s，请确认 k3s 已启动: sudo systemctl start k3s"
  exit 1
fi
ok "K3s 连接正常"

# 检查 env 仓库
ENV_VALUES="${ENV_REPO_DIR}/environments/${DEPLOY_ENV}/values.yaml"
if [[ ! -f "$ENV_VALUES" ]]; then
  error "找不到 env 仓库 values 文件: ${ENV_VALUES}"
  error "请先克隆 env 仓库到 ${ENV_REPO_DIR}:"
  error "  git clone git@github.com:tomturing/hci-platform-env.git ${ENV_REPO_DIR}"
  exit 1
fi
ok "env 仓库 values 文件: ${ENV_VALUES}"

# 提取 env values 中的关键字段（bash 简单解析，无需 yq）
_yaml_get() {
  # 简单 YAML 单行值读取：_yaml_get <key> <file>
  grep -E "^[[:space:]]*${1}[[:space:]]*:" "$2" | head -1 | sed -E 's/.*:[[:space:]]*//' | tr -d '"' | tr -d "'" | tr -d '[:space:]'
}

GHCR_TOKEN="$(_yaml_get ghcrToken "${ENV_VALUES}")"
GHCR_USER="$(_yaml_get imagePullUser "${ENV_VALUES}")"
IMAGE_REGISTRY="$(_yaml_get imageRegistry "${ENV_VALUES}")"

info "imageRegistry  = ${IMAGE_REGISTRY:-（未设置，使用默认）}"
info "imagePullUser  = ${GHCR_USER:-（未设置）}"
info "ghcrToken      = ${GHCR_TOKEN:+（已设置，已脱敏）}"

# ============================================================================
# 步骤 1：配置 K3s 私有镜像仓库认证（registries.yaml）
# ============================================================================
section "步骤 1：配置 K3s ghcr.io 镜像仓库认证"

K3S_REGISTRIES="/etc/rancher/k3s/registries.yaml"

if [[ -n "$GHCR_TOKEN" && -n "$GHCR_USER" ]]; then
  GHCR_REGISTRY_HOST="ghcr.io"
  REGISTRY_CONTENT="mirrors:
  ${GHCR_REGISTRY_HOST}:
    endpoint:
      - \"https://${GHCR_REGISTRY_HOST}\"
configs:
  ${GHCR_REGISTRY_HOST}:
    auth:
      username: ${GHCR_USER}
      password: ${GHCR_TOKEN}
"
  CURRENT_CONTENT=""
  [[ -f "$K3S_REGISTRIES" ]] && CURRENT_CONTENT="$(cat "$K3S_REGISTRIES" 2>/dev/null || true)"

  if [[ "$CURRENT_CONTENT" == "$REGISTRY_CONTENT" ]]; then
    ok "registries.yaml 已是最新，无需更新"
  else
    info "写入 ${K3S_REGISTRIES} ..."
    echo "$REGISTRY_CONTENT" | sudo tee "$K3S_REGISTRIES" > /dev/null
    ok "registries.yaml 已更新，重启 k3s 使其生效..."
    sudo systemctl restart k3s
    info "等待 K3s 重启完成 (20s)..."
    sleep 20
    # 等待节点 Ready
    for i in $(seq 1 30); do
      if ${KUBECTL} get nodes 2>/dev/null | grep -q " Ready"; then
        ok "K3s 节点已就绪"; break
      fi
      [[ $i -eq 30 ]] && { error "K3s 重启超时，请手动检查: sudo systemctl status k3s"; exit 1; }
      sleep 5
    done
  fi
else
  warn "GHCR token 或用户未设置，跳过 registries.yaml 配置"
  warn "如需从 ghcr.io 拉取镜像，请确保 env values 中包含 ghcrToken 和 imagePullUser"
fi

# ============================================================================
# 步骤 2：配置 Traefik 自定义端口（可选）
# ============================================================================
section "步骤 2：Traefik 端口配置（HTTP ${TRAEFIK_HTTP_PORT}）"

TRAEFIK_MANIFEST_DIR="/var/lib/rancher/k3s/server/manifests"
TRAEFIK_CUSTOM="${TRAEFIK_MANIFEST_DIR}/traefik-config.yaml"

if [[ "$SKIP_TRAEFIK" == "true" ]]; then
  warn "已跳过 Traefik 端口配置（--skip-traefik）"
else
  TRAEFIK_CONFIG="apiVersion: helm.cattle.io/v1
kind: HelmChartConfig
metadata:
  name: traefik
  namespace: kube-system
spec:
  valuesContent: |-
    ports:
      web:
        exposedPort: ${TRAEFIK_HTTP_PORT}
    service:
      spec:
        externalTrafficPolicy: Local
"
  info "写入 Traefik 自定义配置: ${TRAEFIK_CUSTOM}"
  sudo mkdir -p "$TRAEFIK_MANIFEST_DIR"
  echo "$TRAEFIK_CONFIG" | sudo tee "$TRAEFIK_CUSTOM" > /dev/null
  ok "Traefik HelmChartConfig 已写入（K3s 将在后台自动应用，可能需要 60s）"
fi

# ============================================================================
# 步骤 3：获取 WSL IP 并设定 publicUrl
# ============================================================================
section "步骤 3：确定访问域名"

WSL_IP="$(hostname -I | awk '{print $1}')"
info "WSL 本机 IP: ${WSL_IP}"

if [[ "$NO_NIPIO" == "true" ]]; then
  PUBLIC_URL=""
  info "已启用 --no-nipio 模式，Ingress 将不限制 host，可通过裸 IP 访问"
  ACCESS_URL="http://${WSL_IP}:${TRAEFIK_HTTP_PORT}"
else
  NIP_DOMAIN="${WSL_IP}.nip.io"
  PUBLIC_URL="http://${NIP_DOMAIN}:${TRAEFIK_HTTP_PORT}"
  info "nip.io 域名: ${NIP_DOMAIN}"
  info "访问地址: ${PUBLIC_URL}"
  ACCESS_URL="${PUBLIC_URL}"
fi

# ============================================================================
# 步骤 4：创建命名空间
# ============================================================================
section "步骤 4：创建命名空间 ${NAMESPACE}"

if ${KUBECTL} get namespace "$NAMESPACE" &>/dev/null; then
  ok "命名空间 ${NAMESPACE} 已存在"
else
  ${KUBECTL} create namespace "$NAMESPACE"
  ok "命名空间 ${NAMESPACE} 已创建"
fi

# ============================================================================
# helm_upgrade 辅助函数
# ============================================================================
helm_upgrade() {
  local release="$1"; shift
  local chart="$1"; shift
  info "Helm install/upgrade: ${release} ← ${chart}"
  KUBECONFIG="$HELM_KUBECONFIG" helm upgrade --install "$release" "$chart" \
    --namespace "$NAMESPACE" \
    --create-namespace \
    --timeout 10m \
    --cleanup-on-fail \
    "$@"
}

# ============================================================================
# A-3: pre_deploy_cleanup — 清理 scheduler 动态创建的孤立 Pod
# ============================================================================
# scheduler-service 用 managed-by=scheduler-service 打标，Helm 无法追踪这些 Pod。
# 每次部署前主动清理，防止孤立 Pod 占用宿主机端口或残留错误配置。
pre_deploy_cleanup() {
  local ns="$1"
  local count
  count=$(${KUBECTL} get pod -l managed-by=scheduler-service -n "${ns}" \
    --ignore-not-found --no-headers 2>/dev/null | wc -l || echo 0)
  if [[ "$count" -gt 0 ]]; then
    warn "[PRE-DEPLOY] 发现 ${count} 个 scheduler 孤立 Pod，开始清理..."
    ${KUBECTL} delete pod \
      -l managed-by=scheduler-service \
      -n "${ns}" \
      --ignore-not-found \
      --grace-period=10 \
      --wait=false
    ok "[PRE-DEPLOY] 孤立 Pod 删除请求已发送（后台继续终止）"
  else
    info "[PRE-DEPLOY] 无 scheduler 孤立 Pod，跳过清理"
  fi
}

# ============================================================================
# C-3: check_nodeport_conflicts — NodePort 冲突预检
# ============================================================================
# 在部署前检测目标 Helm Chart 将使用的 NodePort 是否已被其他 namespace 占用。
# 防止因端口冲突导致 Service 创建失败或静默复用错误端口。
check_nodeport_conflicts() {
  local chart_path="$1"
  local values_file="$2"
  local target_ns="$3"

  info "[C-3] 检测 NodePort 冲突..."

  # 渲染 chart 获取将使用的 NodePort 列表
  local target_ports
  target_ports=$(KUBECONFIG="$HELM_KUBECONFIG" helm template hci-precheck "${chart_path}" \
    -f "${values_file}" \
    --namespace "${target_ns}" 2>/dev/null \
    | grep -oE "nodePort: [0-9]+" \
    | grep -oE "[0-9]+" \
    || true)

  if [[ -z "${target_ports}" ]]; then
    info "[C-3] Chart 未使用 NodePort，跳过冲突检测"
    return 0
  fi

  local conflict_found=0
  while IFS= read -r port; do
    [[ -z "${port}" ]] && continue
    # 查找除目标 namespace 之外的 NodePort 占用
    local conflict
    conflict=$(${KUBECTL} get svc -A --no-headers 2>/dev/null \
      | grep -v "^${target_ns} " \
      | awk '{print $6}' \
      | grep ":${port}/" \
      || true)
    if [[ -n "${conflict}" ]]; then
      error "[C-3] NodePort ${port} 已被其他 namespace 占用："
      error "      ${conflict}"
      error "      请先执行 helm uninstall 或修改目标 NodePort 配置"
      conflict_found=1
    fi
  done <<< "${target_ports}"

  if [[ "${conflict_found}" -eq 0 ]]; then
    ok "[C-3] NodePort 无冲突"
  else
    error "[C-3] NodePort 冲突检测失败，部署中止"
    exit 1
  fi
}

# ============================================================================
# 步骤 5：部署 hci-platform-infra（集群级 StorageClass + RBAC）
# ============================================================================
section "步骤 5：部署 hci-platform-infra（StorageClass + RBAC）"

INFRA_CHART="${PROJECT_ROOT}/deploy/helm/hci-platform-infra"

# infra chart 是集群级资源，不绑定 namespace（但 helm release 仍需一个 namespace）
info "部署集群级资源..."
KUBECONFIG="$HELM_KUBECONFIG" helm upgrade --install hci-platform-infra "$INFRA_CHART" \
  --namespace kube-system \
  --create-namespace \
  --timeout 5m \
  --atomic \
  --cleanup-on-fail \
  || warn "hci-platform-infra 部署失败（可能是集群级资源权限问题，继续...）"
ok "hci-platform-infra 完成"

[[ "$INFRA_ONLY" == "true" ]] && { ok "已设置 --infra-only，部署结束"; exit 0; }

# A-3: 部署前清理 scheduler 孤立 Pod
pre_deploy_cleanup "$NAMESPACE"

# C-3: NodePort 冲突预检（仅当 helm 可用时检测）
check_nodeport_conflicts "${APP_CHART}" "${APP_VALUES}" "${NAMESPACE}"

# ============================================================================
# 步骤 6：部署 hci-platform（主业务 — Secret / ConfigMap / 服务）
# ============================================================================
section "步骤 6：部署 hci-platform（业务服务）"

APP_CHART="${PROJECT_ROOT}/deploy/helm/hci-platform"
APP_VALUES="${APP_CHART}/values.yaml"

# ---- 检测现有部署，自动决定 dataLayer.manage --------------------------------
# 若 postgres StatefulSet 已由 hci-platform 管理，保持 manage=true 避免数据丢失
# 若是全新环境，使用 manage=false（数据层由 hci-platform-data 独立 chart 管理）
if [[ -n "$FORCE_DATALAYER_MANAGE" ]]; then
  DATALAYER_MANAGE="$FORCE_DATALAYER_MANAGE"
  info "dataLayer.manage 强制设置为 $DATALAYER_MANAGE（--datalayer-manage 参数）"
elif ${KUBECTL} -n "$NAMESPACE" get statefulset postgres &>/dev/null 2>&1; then
  # 检查该 StatefulSet 是否由 hci-platform release 管理（而非 hci-platform-data）
  MANAGING_RELEASE=$(${KUBECTL} -n "$NAMESPACE" get statefulset postgres \
    -o jsonpath='{.metadata.labels.app\.kubernetes\.io/managed-by}' 2>/dev/null || true)
  if helm --kubeconfig "$HELM_KUBECONFIG" get values hci-platform -n "$NAMESPACE" &>/dev/null 2>&1; then
    DATALAYER_MANAGE="true"
    warn "检测到 postgres 已由 hci-platform 管理，设置 dataLayer.manage=true 以保护现有数据"
    warn "如需迁移到独立 data chart，请先备份数据再运行: --datalayer-manage false"
  else
    DATALAYER_MANAGE="false"
    info "全新部署，dataLayer.manage=false（数据层由 hci-platform-data 管理）"
  fi
else
  DATALAYER_MANAGE="false"
  info "未检测到现有 postgres，dataLayer.manage=false（数据层由 hci-platform-data 管理）"
fi

# ---- 检测 postgres 密码是否需要同步 ----------------------------------------
if [[ "$DATALAYER_MANAGE" == "true" ]] && \
   ${KUBECTL} -n "$NAMESPACE" get pod postgres-0 &>/dev/null 2>&1; then
  # 读取 env-repo 中的新密码
  NEW_PG_PASS="$(_yaml_get postgresPassword "${ENV_VALUES}")"
  # 读取当前 K8s Secret 中的密码
  CURRENT_PG_PASS=$(${KUBECTL} -n "$NAMESPACE" get secret hci-secrets \
    -o jsonpath='{.data.POSTGRES_PASSWORD}' 2>/dev/null | base64 -d 2>/dev/null || echo "")

  if [[ -n "$NEW_PG_PASS" && "$CURRENT_PG_PASS" != "$NEW_PG_PASS" ]]; then
    info "检测到 postgres 密码变化，同步更新数据库内部密码..."
    PG_USER="$(_yaml_get postgresUser "${ENV_VALUES}")"
    PG_USER="${PG_USER:-hci_admin}"
    if ${KUBECTL} -n "$NAMESPACE" exec postgres-0 -- \
        psql -U "$PG_USER" -c "ALTER USER ${PG_USER} PASSWORD '${NEW_PG_PASS}';" 2>/dev/null; then
      ok "postgres 密码同步成功"
    else
      warn "postgres 密码同步失败（postgres Pod 可能未就绪），将在 Helm 升级后自动生效"
    fi
  else
    info "postgres 密码无变化，跳过同步"
  fi
fi

# ---- 检查 openclaw 仓库是否存在 ------------------------------------------------
OPENCLAW_REPO="${PROJECT_ROOT}/../openclaw"
OVERRIDE_FILE=""

# 检查 .local/values-dualrepo-local.yaml 是否存在（本地持久化覆盖）
LOCAL_OVERRIDE="${PROJECT_ROOT}/.local/values-dualrepo-local.yaml"
if [[ -f "$LOCAL_OVERRIDE" ]]; then
  info "使用本地覆盖文件: ${LOCAL_OVERRIDE}"
  OVERRIDE_FILE="$LOCAL_OVERRIDE"
else
  # 动态生成临时覆盖文件（避免 --set {} 被 Helm 解析为 YAML 列表的问题）
  TMP_OVERRIDE="$(mktemp /tmp/hci-dualrepo-XXXXXX.yaml)"
  trap "rm -f '$TMP_OVERRIDE'" EXIT

  REGISTRY_JSON='{}'
  if [[ -d "${OPENCLAW_REPO}" ]]; then
    warn "检测到 openclaw 仓库但使用默认空池配置，如需激活助手池请手动配置 .local/values-dualrepo-local.yaml"
  else
    warn "未找到 openclaw 仓库 (${OPENCLAW_REPO})，自动禁用 openclaw/learningclaw/productionclaw"
  fi

  cat > "$TMP_OVERRIDE" << __OVERRIDE_EOF
# 自动生成的双仓模式临时覆盖（run: $(date '+%Y-%m-%d %H:%M:%S')）
global:
  publicUrl: "${PUBLIC_URL}"
dataLayer:
  manage: ${DATALAYER_MANAGE}
clusterResources:
  manage: false
observabilityLayer:
  manage: false
openclaw:
  enabled: false
learningclaw:
  enabled: false
productionclaw:
  enabled: false
config:
  assistantRegistryJson: '${REGISTRY_JSON}'
__OVERRIDE_EOF

  OVERRIDE_FILE="$TMP_OVERRIDE"
  info "临时覆盖文件: ${TMP_OVERRIDE}"
fi

# publicUrl 参数（仅在非 .local 覆盖文件时通过 --set 补充）
EXTRA_ARGS=()
if [[ -n "$LOCAL_OVERRIDE" && -f "$LOCAL_OVERRIDE" ]]; then
  # 本地覆盖文件已包含所有覆盖，不需额外参数
  true
elif [[ -n "$PUBLIC_URL" ]]; then
  EXTRA_ARGS=("--set" "global.publicUrl=${PUBLIC_URL}")
fi

# dataLayer 由独立 chart 管理（禁止主 chart 创建 postgres/redis）
helm_upgrade hci-platform "$APP_CHART" \
  -f "$APP_VALUES" \
  -f "$ENV_VALUES" \
  -f "$OVERRIDE_FILE" \
  "${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}"

ok "hci-platform 部署完成"

# ============================================================================
# 步骤 6.5：清理孤立的 pool Pod（DEPLOY-004）
# ============================================================================
# scheduler-service 动态创建的 pool Pod 不由 Helm 管理，镜像变更后旧 Pod 会持续
# 以 ErrImagePull / ImagePullBackOff 状态存在，误导排查。每次部署后自动清理。
section "步骤 6.5：清理孤立 pool Pod"

ORPHAN_POOL_PODS=$(${KUBECTL} -n "$NAMESPACE" get pods --no-headers 2>/dev/null \
  | awk '$1 ~ /-pool-/ && ($3=="ErrImagePull" || $3=="ImagePullBackOff" || $3 ~ /^Init:/) {print $1}' \
  | tr '\n' ' ' || true)

if [[ -n "${ORPHAN_POOL_PODS// /}" ]]; then
  warn "检测到孤立失败 pool Pod，正在清理: ${ORPHAN_POOL_PODS}"
  # shellcheck disable=SC2086
  ${KUBECTL} -n "$NAMESPACE" delete pod ${ORPHAN_POOL_PODS} --ignore-not-found || true
  ok "孤立 pool Pod 清理完成"
else
  ok "无孤立 pool Pod 需要清理"
fi

# ============================================================================
# 步骤 7：部署 hci-platform-data（PostgreSQL + Redis）
# ============================================================================
section "步骤 7：部署 hci-platform-data（PostgreSQL + Redis）"

DATA_CHART="${PROJECT_ROOT}/deploy/helm/hci-platform-data"

if [[ "$DATALAYER_MANAGE" == "true" ]]; then
  info "dataLayer.manage=true，postgres/redis 由 hci-platform 主 chart 管理，跳过 hci-platform-data"
else
  # 等待 hci-secrets ConfigMap/Secret 就绪（由 hci-platform 创建）
  info "等待 hci-secrets 就绪..."
  for i in $(seq 1 30); do
    if ${KUBECTL} -n "$NAMESPACE" get secret hci-secrets &>/dev/null; then
      ok "hci-secrets 已就绪"; break
    fi
    [[ $i -eq 30 ]] && { error "hci-secrets 未在预期时间内创建，请检查 hci-platform 部署"; exit 1; }
    sleep 3
  done

  helm_upgrade hci-platform-data "$DATA_CHART" \
    -f "${DATA_CHART}/values.yaml"

  ok "hci-platform-data 部署完成"
fi

# ============================================================================
# 步骤 8：等待 Pod 就绪
# ============================================================================
section "步骤 8：等待 Pod 就绪"

info "等待业务 Pod 启动（最长 5 分钟）..."
${KUBECTL} -n "$NAMESPACE" rollout status deployment/api-gateway --timeout=5m || warn "api-gateway 未就绪"
${KUBECTL} -n "$NAMESPACE" rollout status deployment/case-service --timeout=5m || warn "case-service 未就绪"
${KUBECTL} -n "$NAMESPACE" rollout status deployment/conversation-service --timeout=5m || warn "conversation-service 未就绪"
${KUBECTL} -n "$NAMESPACE" rollout status deployment/scheduler-service --timeout=5m || warn "scheduler-service 未就绪"

info "等待数据库 Pod 启动..."
${KUBECTL} -n "$NAMESPACE" rollout status statefulset/postgres --timeout=5m || warn "postgres 未就绪"
${KUBECTL} -n "$NAMESPACE" rollout status statefulset/redis --timeout=5m || warn "redis 未就绪"

# ============================================================================
# 步骤 8.5：自动执行数据库迁移脚本（幂等，全部使用 IF NOT EXISTS）
# ============================================================================
# 解决 DUAL-006：本地 PVC 复用场景下，schema 可能落后于代码版本导致运行时报错。
# 迁移脚本均幂等，可安全重复执行（已存在字段/表会跳过）。
section "步骤 8.5：数据库迁移"

PG_POD="postgres-0"
PG_USER=$(_yaml_get postgresUser "${ENV_VALUES}" 2>/dev/null || echo "")
PG_USER="${PG_USER:-hci_admin}"
PG_DB="hci_troubleshoot"

# 确认迁移脚本列表（按依赖顺序）
MIGRATION_SCRIPTS=(
  "database/migrate_evaluation_v1.sql"
  "database/migrate_kb_v3.sql"
  "database/migrate_p4_v1.sql"
  "database/migrate_tool_audit_log.sql"
  "database/migrate_conversation_p4_v1.sql"   # P4 conversation 字段 + prompt_audit.context_breakdown
)

# 等待 postgres 就绪（最长 90 秒）
POSTGRES_READY=false
for i in $(seq 1 18); do
  if ${KUBECTL} -n "$NAMESPACE" exec "$PG_POD" -- \
      pg_isready -U "$PG_USER" -d "$PG_DB" &>/dev/null 2>&1; then
    POSTGRES_READY=true; break
  fi
  info "等待 postgres 就绪... ($i/18, 每次 5s)"
  sleep 5
done

if ! $POSTGRES_READY; then
  warn "postgres 未在 90 秒内就绪，跳过自动迁移"
  warn "请数据库就绪后手动执行："
  for sql_file in "${MIGRATION_SCRIPTS[@]}"; do
    warn "  kubectl exec -i -n ${NAMESPACE} ${PG_POD} -- psql -U ${PG_USER} -d ${PG_DB} < ${PROJECT_ROOT}/${sql_file}"
  done
else
  MIGRATION_ERRORS=0
  for sql_file in "${MIGRATION_SCRIPTS[@]}"; do
    full_path="${PROJECT_ROOT}/${sql_file}"
    if [[ ! -f "$full_path" ]]; then
      warn "迁移脚本不存在，跳过: ${sql_file}"
      continue
    fi
    info "执行迁移: $(basename "${sql_file}") ..."
    MIGRATION_OUT=$(${KUBECTL} -n "$NAMESPACE" exec -i "$PG_POD" -- \
        psql -U "$PG_USER" -d "$PG_DB" --set ON_ERROR_STOP=off \
        < "$full_path" 2>&1 || true)
    # NOTICE（如 "already exists"）属正常，只在有 ERROR 时才告警
    if echo "$MIGRATION_OUT" | grep -qE "^ERROR:"; then
      warn "迁移 $(basename "${sql_file}") 存在 ERROR（通常是已存在对象，无害）:"
      echo "$MIGRATION_OUT" | grep "^ERROR:" | head -3 | while read -r line; do warn "  $line"; done
      MIGRATION_ERRORS=$((MIGRATION_ERRORS+1))
    else
      ok "迁移完成: $(basename "${sql_file}")"
    fi
  done

  if [[ $MIGRATION_ERRORS -gt 0 ]]; then
    warn "有 ${MIGRATION_ERRORS} 个迁移脚本报 ERROR，请检查以上日志后手动确认"
  else
    ok "所有数据库迁移执行完成（共 ${#MIGRATION_SCRIPTS[@]} 个脚本）"
  fi
fi

# ============================================================================
# 步骤 9：部署状态汇总
# ============================================================================
section "步骤 9：部署状态"

echo ""
${KUBECTL} -n "$NAMESPACE" get pods -o wide
echo ""

# 打印 Helm releases
echo -e "\n${CYAN}Helm Releases:${NC}"
KUBECONFIG="$HELM_KUBECONFIG" helm list -A 2>/dev/null || true

echo ""
echo -e "${GREEN}╔══════════════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║              🎉 HCI 平台部署完成！                              ║${NC}"
echo -e "${GREEN}╠══════════════════════════════════════════════════════════════╣${NC}"
echo -e "${GREEN}║  访问地址:  ${ACCESS_URL}${NC}"
echo -e "${GREEN}║  API:       ${ACCESS_URL}/api/v1/health${NC}"
echo -e "${GREEN}║  管理后台:  ${ACCESS_URL}/admin/${NC}"
echo -e "${GREEN}║  客户端:    ${ACCESS_URL}/${NC}"
echo -e "${GREEN}║${NC}"
echo -e "${GREEN}║  Traefik 端口: ${TRAEFIK_HTTP_PORT}${NC}"
echo -e "${GREEN}║  命名空间:     ${NAMESPACE}${NC}"
echo -e "${GREEN}║${NC}"
echo -e "${GREEN}║  注意: 若网络环境无法解析 nip.io，请使用 --no-nipio 参数，${NC}"
echo -e "${GREEN}║        并直接通过 http://${WSL_IP}:${TRAEFIK_HTTP_PORT} 访问。${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════════════════════════╝${NC}"
echo ""
info "查看 Pod 详情: ${KUBECTL} -n ${NAMESPACE} get pods"
info "查看日志:      ${KUBECTL} -n ${NAMESPACE} logs deployment/api-gateway -f"
info "回滚:          KUBECONFIG=${HELM_KUBECONFIG} helm -n ${NAMESPACE} rollback hci-platform"
