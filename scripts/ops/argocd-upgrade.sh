#!/usr/bin/env bash
set -euo pipefail

# =============================================================================
# ArgoCD 升级脚本
#
# 功能：
# - 升级前备份关键配置
# - 健康检查
# - 版本升级
# - 升级后验证
# - 记录升级历史
# =============================================================================

ARGOCD_VERSION="${ARGOCD_VERSION:-v3.3.6}"
KUBECTL="${KUBECTL:-kubectl}"
WATCHDOG_MANIFEST="${WATCHDOG_MANIFEST:-deploy/gitops/argocd-ops/argocd-repo-server-copyutil-watchdog.yaml}"
BACKUP_DIR="${BACKUP_DIR:-/tmp/argocd-backup}"
NO_BACKUP="${NO_BACKUP:-false}"

TRACE_ID="hci-argocd-upgrade-$(date +%Y%m%d%H%M%S)-$RANDOM"

info() { echo "[INFO][${TRACE_ID}] $*"; }
warn() { echo "[WARN][${TRACE_ID}] $*" >&2; }
error() { echo "[ERROR][${TRACE_ID}] $*" >&2; }

usage() {
  cat <<'EOF'
用法：
  bash scripts/ops/argocd-upgrade.sh

可选环境变量：
  ARGOCD_VERSION   ArgoCD 版本（默认：v3.3.6）
  KUBECTL          kubectl 命令（默认：kubectl）
  WATCHDOG_MANIFEST watchdog 清单路径
  BACKUP_DIR       备份目录（默认：/tmp/argocd-backup）
  NO_BACKUP        跳过备份（默认：false）

示例：
  ARGOCD_VERSION=v3.4.0 bash scripts/ops/argocd-upgrade.sh
  NO_BACKUP=true bash scripts/ops/argocd-upgrade.sh  # 跳过备份
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

# 检查 kubectl
if ! command -v "${KUBECTL%% *}" >/dev/null 2>&1; then
  error "未找到 kubectl 命令：${KUBECTL}"
  exit 1
fi

# 获取当前版本
get_current_version() {
  ${KUBECTL} get deployment argocd-server -n argocd -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null | sed 's/.*://' || echo "unknown"
}

# 健康检查
health_check() {
  info "执行升级前健康检查..."

  # 检查所有 Pods 是否 Running
  local not_ready
  not_ready=$(${KUBECTL} get pods -n argocd -o jsonpath='{range .items[?(@.status.phase!="Running")]}{.metadata.name}{"\n"}{end}' 2>/dev/null)
  if [[ -n "$not_ready" ]]; then
    error "以下 Pod 未处于 Running 状态："
    echo "$not_ready"
    return 1
  fi

  # 检查 Application 健康
  local unhealthy
  unhealthy=$(${KUBECTL} get application -n argocd -o jsonpath='{range .items[?(@.status.health.status!="Healthy")]}{.metadata.name}{"\n"}{end}' 2>/dev/null)
  if [[ -n "$unhealthy" ]]; then
    warn "以下 Application 未处于 Healthy 状态："
    echo "$unhealthy"
    warn "继续升级可能导致问题，建议先修复"
    read -r -p "是否继续？(y/N) " confirm
    [[ "$confirm" =~ ^[Yy]$ ]] || exit 1
  fi

  info "健康检查通过"
}

# 备份
backup() {
  if [[ "$NO_BACKUP" == "true" ]]; then
    warn "跳过备份（NO_BACKUP=true）"
    return 0
  fi

  local backup_path="${BACKUP_DIR}/$(date +%Y%m%d_%H%M%S)"
  mkdir -p "$backup_path"

  info "备份 ArgoCD 配置到 ${backup_path}"

  # 备份 Secrets
  ${KUBECTL} get secret -n argocd argocd-secret -o yaml > "${backup_path}/argocd-secret.yaml" 2>/dev/null || warn "无法备份 argocd-secret"
  ${KUBECTL} get secret -n argocd argocd-initial-admin-secret -o yaml > "${backup_path}/argocd-initial-admin-secret.yaml" 2>/dev/null || true

  # 备份 Applications
  ${KUBECTL} get application -n argocd -o yaml > "${backup_path}/applications.yaml"

  # 备份 Repo Credentials
  ${KUBECTL} get secret -n argocd -l argocd.argoproj.io/secret-type=repo-creds -o yaml > "${backup_path}/repo-creds.yaml" 2>/dev/null || true

  # 记录当前版本
  get_current_version > "${backup_path}/current_version.txt"

  info "备份完成"
}

# 升级
upgrade() {
  local current_version
  current_version=$(get_current_version)

  info "当前版本: ${current_version}"
  info "目标版本: ${ARGOCD_VERSION}"

  if [[ "$current_version" == "$ARGOCD_VERSION" ]]; then
    warn "目标版本与当前版本相同，跳过升级"
    return 0
  fi

  local install_url="https://raw.githubusercontent.com/argoproj/argo-cd/${ARGOCD_VERSION}/manifests/install.yaml"
  info "安装清单：${install_url}"

  ${KUBECTL} create namespace argocd --dry-run=client -o yaml | ${KUBECTL} apply -f -

  # 使用 server-side apply + force-conflicts，防止字段管理器冲突（PIT: P1）
  ${KUBECTL} apply --server-side --force-conflicts -n argocd -f "${install_url}"
}

# 升级后补丁：恢复被 install.yaml 覆盖的自定义配置
# 背景：ArgoCD install.yaml 升级会覆盖手动 patch 的 Deployment 配置，需升级后重新应用
# 参考：docs/deploy/pitfalls/k8s.md D-003, D-004
post_upgrade_patch() {
  info "应用升级后补丁（恢复自定义配置）..."

  # ── 1. 预创建 watchdog SA/RBAC（解决 PreSync Job SA 鸡蛋问题，D-003）──
  if [[ -f "${WATCHDOG_MANIFEST}" ]]; then
    info "预创建 watchdog SA/RBAC：${WATCHDOG_MANIFEST}"
    ${KUBECTL} apply -f "${WATCHDOG_MANIFEST}"
  else
    warn "未找到 watchdog 清单，跳过 SA 预创建：${WATCHDOG_MANIFEST}"
  fi

  # ── 2. 恢复 NodePort 访问（install.yaml 会把 argocd-server 重置为 ClusterIP）──
  local nodeport_manifest
  nodeport_manifest="$(dirname "${WATCHDOG_MANIFEST}")/argocd-server-nodeport.yaml"
  if [[ -f "${nodeport_manifest}" ]]; then
    info "恢复 NodePort Service：${nodeport_manifest}"
    ${KUBECTL} apply -f "${nodeport_manifest}"
  else
    warn "未找到 NodePort 清单，跳过：${nodeport_manifest}"
  fi

  # ── 3. repo-server：注入 REDIS_PASSWORD（解决 Redis cache EOF，D-004）──
  # 检查是否已存在，避免重复添加
  local has_redis_pw
  has_redis_pw=$(${KUBECTL} get deployment argocd-repo-server -n argocd \
    -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="REDIS_PASSWORD")].name}' 2>/dev/null)
  if [[ -z "$has_redis_pw" ]]; then
    info "注入 REDIS_PASSWORD 环境变量..."
    ${KUBECTL} patch deployment argocd-repo-server -n argocd --type='json' -p='[
      {"op":"add","path":"/spec/template/spec/containers/0/env/-",
       "value":{"name":"REDIS_PASSWORD","valueFrom":{"secretKeyRef":{"key":"auth","name":"argocd-redis"}}}}
    ]'
  else
    info "REDIS_PASSWORD 已存在，跳过"
  fi

  # ── 4. repo-server：设置 ARGOCD_EXEC_TIMEOUT（防止 git fetch 90s 超时）──
  local has_exec_timeout
  has_exec_timeout=$(${KUBECTL} get deployment argocd-repo-server -n argocd \
    -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="ARGOCD_EXEC_TIMEOUT")].name}' 2>/dev/null)
  if [[ -z "$has_exec_timeout" ]]; then
    info "注入 ARGOCD_EXEC_TIMEOUT=600s..."
    ${KUBECTL} patch deployment argocd-repo-server -n argocd --type='json' -p='[
      {"op":"add","path":"/spec/template/spec/containers/0/env/-",
       "value":{"name":"ARGOCD_EXEC_TIMEOUT","value":"600s"}}
    ]'
  else
    info "ARGOCD_EXEC_TIMEOUT 已存在，跳过"
  fi

  # ── 5. repo-server：dnsPolicy:None + 自定义 nameservers（D-004 git 网络）──
  local dns_policy
  dns_policy=$(${KUBECTL} get deployment argocd-repo-server -n argocd \
    -o jsonpath='{.spec.template.spec.dnsPolicy}' 2>/dev/null)
  if [[ "$dns_policy" != "None" ]]; then
    info "设置 repo-server dnsPolicy:None + nameservers..."
    ${KUBECTL} patch deployment argocd-repo-server -n argocd --type='json' -p='[
      {"op":"replace","path":"/spec/template/spec/dnsPolicy","value":"None"},
      {"op":"add","path":"/spec/template/spec/dnsConfig","value":{
        "nameservers":["223.5.5.5","8.8.8.8","10.43.0.10"],
        "options":[{"name":"ndots","value":"1"}],
        "searches":["argocd.svc.cluster.local","svc.cluster.local","cluster.local"]
      }}
    ]'
  else
    info "dnsPolicy:None 已设置，跳过"
  fi

  # ── 6. argocd-cmd-params-cm：补充 git 超时配置 ──
  ${KUBECTL} patch cm argocd-cmd-params-cm -n argocd --type merge -p \
    '{"data":{"reposerver.git.request.timeout":"600s","reposerver.parallelism.limit":"4"}}'
  info "argocd-cmd-params-cm 已更新"

  # ── 7. 等待 repo-server 完成 rollout ──
  info "等待 repo-server rollout 完成..."
  ${KUBECTL} rollout status deployment/argocd-repo-server -n argocd --timeout=120s

  info "升级后补丁全部应用完成"
}

# 等待就绪
wait_ready() {
  info "等待核心组件就绪..."
  ${KUBECTL} -n argocd rollout status deployment/argocd-server --timeout=300s
  ${KUBECTL} -n argocd rollout status deployment/argocd-repo-server --timeout=300s
  ${KUBECTL} -n argocd rollout status statefulset/argocd-application-controller --timeout=300s
}

# 验证
verify() {
  info "升级后验证..."

  # 检查版本
  local new_version
  new_version=$(get_current_version)
  info "当前镜像版本：${new_version}"

  # 检查 Pods
  ${KUBECTL} get pods -n argocd -o wide

  # 检查 Applications
  local app_count
  app_count=$(${KUBECTL} get application -n argocd --no-headers 2>/dev/null | wc -l)
  info "Application 数量：${app_count}"

  info "验证完成"
}

# 记录升级历史
log_history() {
  local history_file="${BACKUP_DIR}/upgrade_history.log"
  mkdir -p "${BACKUP_DIR}"
  echo "$(date '+%Y-%m-%d %H:%M:%S') | ${TRACE_ID} | $(get_current_version) -> ${ARGOCD_VERSION}" >> "$history_file"
  info "升级历史已记录到 ${history_file}"
}

# 主流程
main() {
  info "========== ArgoCD 升级开始 =========="

  health_check
  backup
  upgrade
  wait_ready
  post_upgrade_patch

  if [[ -f "${WATCHDOG_MANIFEST}" ]]; then
    info "watchdog 清单已在 post_upgrade_patch 中应用，跳过重复 apply"
  fi

  verify
  log_history

  info "========== ArgoCD 升级完成 =========="
  info "版本：${ARGOCD_VERSION}"
}

main "$@"