#!/bin/bash
# =============================================================================
# HCI 平台 — K8s 部署验证脚本
# =============================================================================
# 使用方法: bash scripts/k3s-verify.sh
#
set -euo pipefail

NAMESPACE="hci-troubleshoot"
OBS_NAMESPACE="hci-observability"
KUBECTL="sudo k3s kubectl"

# 颜色
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

info()  { echo -e "${BLUE}[INFO]${NC}  $*"; }
ok()    { echo -e "${GREEN}[✅]${NC}   $*"; }
fail()  { echo -e "${RED}[❌]${NC}   $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }

PASS=0
FAIL=0

check() {
  local desc="$1"
  shift
  if "$@" >/dev/null 2>&1; then
    ok "$desc"
    PASS=$((PASS + 1))
  else
    fail "$desc"
    FAIL=$((FAIL + 1))
  fi
}

# ============================================================================
# 1. Pod 状态检查
# ============================================================================
echo ""
echo "=========================================="
info "1. Pod 状态检查"
echo "=========================================="

echo ""
info "--- 业务 namespace (${NAMESPACE}) ---"
${KUBECTL} get pods -n "${NAMESPACE}" -o wide
echo ""

# 检查各服务 Pod 是否 Running
for svc in postgres redis api-gateway case-service conversation-service scheduler-service customer-ui admin-ui openclaw; do
  check "Pod ${svc} 运行中" ${KUBECTL} get pods -n "${NAMESPACE}" -l "app.kubernetes.io/name=${svc}" --field-selector=status.phase=Running -o name
done

echo ""
info "--- 可观测性 namespace (${OBS_NAMESPACE}) ---"
${KUBECTL} get pods -n "${OBS_NAMESPACE}" -o wide 2>/dev/null || warn "可观测性 namespace 不存在"

# ============================================================================
# 2. Service 检查
# ============================================================================
echo ""
echo "=========================================="
info "2. Service 检查"
echo "=========================================="

for svc in postgres redis api-gateway case-service conversation-service scheduler-service customer-ui admin-ui openclaw; do
  check "Service ${svc} 存在" ${KUBECTL} get svc "${svc}" -n "${NAMESPACE}"
done

# ============================================================================
# 3. 健康端点检查 (通过 port-forward)
# ============================================================================
echo ""
echo "=========================================="
info "3. 健康端点检查 (port-forward)"
echo "=========================================="

# 临时 port-forward
info "建立 port-forward 到 api-gateway..."
${KUBECTL} port-forward svc/api-gateway 18000:8000 -n "${NAMESPACE}" &
PF_PID=$!
sleep 3

if kill -0 $PF_PID 2>/dev/null; then
  API_BASE="http://localhost:18000"
  
  # API Gateway 健康检查
  check "API Gateway /health" curl -sf "${API_BASE}/health"
  
  # 通过 Gateway 访问后端服务
  check "GET /api/cases (列表)" curl -sf "${API_BASE}/api/cases?client_id=k8s-verify-test"
  
  # 尝试创建工单 (E2E)
  info "E2E: 创建测试工单..."
  CASE_RESP=$(curl -sf -X POST "${API_BASE}/api/cases/" \
    -H "Content-Type: application/json" \
    -d '{"client_id":"k8s-verify-test","title":"K8s验证工单","description":"自动化验证测试"}' 2>/dev/null || echo "")
  
  if [[ -n "$CASE_RESP" ]] && echo "$CASE_RESP" | grep -q "case_id"; then
    CASE_ID=$(echo "$CASE_RESP" | grep -o '"case_id":"[^"]*' | cut -d'"' -f4)
    ok "创建工单成功: ${CASE_ID}"
    PASS=$((PASS + 1))
    
    # 查询工单
    check "查询工单 ${CASE_ID}" curl -sf "${API_BASE}/api/cases/${CASE_ID}"
    
    # 关闭测试工单
    curl -sf -X PUT "${API_BASE}/api/cases/${CASE_ID}/close" >/dev/null 2>&1 && \
      info "  测试工单 ${CASE_ID} 已关闭" || true
  else
    fail "创建工单失败"
    FAIL=$((FAIL + 1))
    [[ -n "$CASE_RESP" ]] && echo "  Response: $CASE_RESP"
  fi
  
  # 清理 port-forward
  kill $PF_PID 2>/dev/null || true
  wait $PF_PID 2>/dev/null || true
else
  fail "port-forward 建立失败"
  FAIL=$((FAIL + 1))
fi

# ============================================================================
# 4. Ingress 检查
# ============================================================================
echo ""
echo "=========================================="
info "4. Ingress 检查"
echo "=========================================="

check "Ingress 资源存在" ${KUBECTL} get ingress -n "${NAMESPACE}" -o name

echo ""
info "Ingress 详情:"
${KUBECTL} get ingress -n "${NAMESPACE}" -o wide 2>/dev/null || true

# ============================================================================
# 5. PVC 检查
# ============================================================================
echo ""
echo "=========================================="
info "5. PVC 检查"
echo "=========================================="

${KUBECTL} get pvc -n "${NAMESPACE}" 2>/dev/null || true
${KUBECTL} get pvc -n "${OBS_NAMESPACE}" 2>/dev/null || true

# ============================================================================
# 结果汇总
# ============================================================================
echo ""
echo "=========================================="
info "验证结果汇总"
echo "=========================================="
echo ""
ok "通过: ${PASS}"
[[ $FAIL -gt 0 ]] && fail "失败: ${FAIL}" || info "失败: 0"
echo ""

if [[ $FAIL -gt 0 ]]; then
  warn "存在 ${FAIL} 项验证失败，请检查上方日志"
  exit 1
else
  ok "所有验证通过! 🎉"
  exit 0
fi
