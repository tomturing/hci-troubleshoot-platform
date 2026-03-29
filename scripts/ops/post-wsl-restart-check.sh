#!/usr/bin/env bash
# =============================================================================
# 🟢 运维脚本 — WSL 重启后自检清单（B-4）
# =============================================================================
# 职责：WSL/K3s 重启后快速验证关键运行时配置，发现问题并提示修复命令
# 使用场景：重启 WSL 或 K3s 服务后第一时间运行
# 使用方法：
#   bash scripts/ops/post-wsl-restart-check.sh
#   bash scripts/ops/post-wsl-restart-check.sh --namespace hci-prod
# 影响范围：🟢 只读检查，不修改集群状态
# =============================================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ─── 颜色定义 ─────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

info()  { echo -e "${BLUE}[INFO]${NC}  $*"; }
ok()    { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; }

# ─── 参数解析 ─────────────────────────────────────────────────
NAMESPACE="${NAMESPACE:-hci-dev}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --namespace|-n) NAMESPACE="$2"; shift 2 ;;
    *) shift ;;
  esac
done

default_kubectl() {
  if [[ "${EUID:-$(id -u)}" -eq 0 ]]; then
    echo "k3s kubectl"
  else
    echo "sudo -n k3s kubectl"
  fi
}
KUBECTL="${KUBECTL:-$(default_kubectl)}"

# 用于统计失败项
FAIL_COUNT=0
WARN_COUNT=0

check_pass()  { ok    "  ✓ $*"; }
check_warn()  { warn  "  ⚠ $*"; WARN_COUNT=$((WARN_COUNT + 1)); }
check_fail()  { error "  ✗ $*"; FAIL_COUNT=$((FAIL_COUNT + 1)); }

# =============================================================================
echo ""
echo -e "${BLUE}╔══════════════════════════════════════╗${NC}"
echo -e "${BLUE}║   WSL 重启后自检清单 (B-4)           ║${NC}"
echo -e "${BLUE}║   Namespace: ${NAMESPACE}               ${NC}"
echo -e "${BLUE}╚══════════════════════════════════════╝${NC}"
echo ""

# ─── [1/5] K3s 服务状态 ───────────────────────────────────────
echo "[1/5] K3s 服务状态..."
if systemctl is-active --quiet k3s 2>/dev/null; then
  check_pass "K3s 服务正在运行"
elif ${KUBECTL} get nodes > /dev/null 2>&1; then
  check_pass "K3s API Server 可访问（非 systemd 启动方式）"
else
  check_fail "K3s 服务不可达"
  echo "       修复：sudo systemctl start k3s"
fi

# ─── [2/5] CoreDNS 配置（GitHub IP 是否存在）─────────────────
echo "[2/5] CoreDNS GitHub IP 配置..."
COREDNS_CM=$(${KUBECTL} get cm coredns -n kube-system -o yaml 2>/dev/null || true)
if echo "${COREDNS_CM}" | grep -q "github.com\|NodeHosts\|192.30"; then
  check_pass "CoreDNS NodeHosts 配置存在"
else
  check_warn "CoreDNS NodeHosts 可能已丢失（重启后 WSL IP 变化会导致 GitHub 访问失败）"
  echo "       修复：${KUBECTL} patch configmap coredns -n kube-system --patch-file scripts/ops/coredns-patch.yaml"
  echo "             ${KUBECTL} rollout restart deployment coredns -n kube-system"
fi

# ─── [3/5] ArgoCD 代理设置（若 ArgoCD 存在）─────────────────
echo "[3/5] ArgoCD 代理配置..."
if ${KUBECTL} get deploy argocd-repo-server -n argocd > /dev/null 2>&1; then
  if ${KUBECTL} get deploy argocd-repo-server -n argocd -o yaml 2>/dev/null | grep -q "HTTPS_PROXY\|HTTP_PROXY"; then
    check_pass "ArgoCD repo-server 代理已配置"
  else
    check_warn "ArgoCD repo-server 未发现代理配置，GitHub 私有仓库同步可能失败"
    echo "       修复：查看 docs/guides/ 中的 ArgoCD 代理配置说明"
  fi
else
  info "  ArgoCD 未部署，跳过"
fi

# ─── [4/5] 核心业务 Pod 状态 ─────────────────────────────────
echo "[4/5] 核心 Pod 状态 (namespace: ${NAMESPACE})..."
if ! ${KUBECTL} get ns "${NAMESPACE}" > /dev/null 2>&1; then
  check_warn "Namespace '${NAMESPACE}' 不存在，可能尚未部署"
else
  NON_RUNNING=$(${KUBECTL} get pods -n "${NAMESPACE}" --no-headers 2>/dev/null | \
    grep -v -E "Running|Completed|Succeeded" | grep -v "^$" || true)
  if [[ -z "${NON_RUNNING}" ]]; then
    TOTAL=$(${KUBECTL} get pods -n "${NAMESPACE}" --no-headers 2>/dev/null | wc -l)
    check_pass "全部 ${TOTAL} 个 Pod 处于 Running/Completed 状态"
  else
    check_warn "存在非 Running Pod："
    echo "${NON_RUNNING}" | sed 's/^/         /'
    echo "       修复：${KUBECTL} get events -n ${NAMESPACE} --sort-by='.lastTimestamp' | tail -20"
  fi
fi

# ─── [5/5] 数据库连接验证 ────────────────────────────────────
echo "[5/5] 数据库连接验证..."
if ${KUBECTL} get pod -n "${NAMESPACE}" -l app=postgres --no-headers 2>/dev/null | grep -q "Running"; then
  PG_POD=$(${KUBECTL} get pod -n "${NAMESPACE}" -l app=postgres --no-headers 2>/dev/null | awk 'NR==1{print $1}')
  if ${KUBECTL} exec -n "${NAMESPACE}" "${PG_POD}" -- \
      psql -U hci_admin -d hci_db -c "SELECT 1;" > /dev/null 2>&1; then
    check_pass "数据库连接正常 (${PG_POD})"
  else
    check_fail "数据库连接失败（可能是密码漂移）"
    echo "       修复：查看 docs/guides/K3s集群运维复盘.md § 密码漂移 章节"
    echo "             kubectl exec -n ${NAMESPACE} ${PG_POD} -- psql -U hci_admin -d hci_db"
  fi
elif ${KUBECTL} get statefulset -n "${NAMESPACE}" postgres --no-headers 2>/dev/null | grep -q "postgres"; then
  check_warn "PostgreSQL StatefulSet 存在但 Pod 未 Running"
else
  info "  未检测到 PostgreSQL Pod，跳过数据库检查"
fi

# ─── 汇总 ─────────────────────────────────────────────────────
echo ""
echo -e "${BLUE}═══════════════════════════════════════${NC}"
if [[ "${FAIL_COUNT}" -gt 0 ]]; then
  echo -e "${RED}自检完成：${FAIL_COUNT} 个错误，${WARN_COUNT} 个警告${NC}"
  echo "请处理上述 [ERROR] 项后重新运行本脚本确认。"
  exit 1
elif [[ "${WARN_COUNT}" -gt 0 ]]; then
  echo -e "${YELLOW}自检完成：0 个错误，${WARN_COUNT} 个警告${NC}"
  echo "建议处理上述 [WARN] 项以确保功能完整。"
  exit 0
else
  echo -e "${GREEN}自检完成：全部检查项通过 ✓${NC}"
  exit 0
fi
