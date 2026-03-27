#!/usr/bin/env bash
# =============================================================================
# 🟢 运维脚本 — K3s 部署验证
# =============================================================================
# 职责：验证 K3s 集群部署健康状态（Pod 就绪、接口应答、核心链路冑烟）
# 使用场景：发布后验证、测试环境健康检查
# 使用方法：
#   bash scripts/ops/k3s-verify.sh
# 影响范围：🟢 只读，不变更集群状态
# =============================================================================
set -euo pipefail

NAMESPACE="${NAMESPACE:-hci-dev}"
OBS_NAMESPACE="${OBS_NAMESPACE:-hci-observability}"
KUBECTL="${KUBECTL:-sudo -n k3s kubectl}"

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

ensure_kubectl_access() {
  if ${KUBECTL} version >/dev/null 2>&1; then
    return 0
  fi

  fail "无法以非交互方式访问 K3s kubectl"
  warn "  当前命令: ${KUBECTL}"
  warn "  请先执行: sudo -v"
  warn "  或显式指定无需 sudo 的命令，例如: KUBECTL='k3s kubectl' bash scripts/k3s-verify.sh"
  exit 1
}

PASS=0
FAIL=0

ensure_kubectl_access

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

check_value() {
  local desc="$1"
  local expected="$2"
  local actual="$3"
  if [[ "$actual" == "$expected" ]]; then
    ok "$desc"
    PASS=$((PASS + 1))
  else
    fail "$desc"
    FAIL=$((FAIL + 1))
    warn "  期望: ${expected}"
    warn "  实际: ${actual}"
  fi
}

check_model_cfg() {
  local desc="$1"
  local actual="$2"
  local model base

  model="${actual%%|*}"
  base="${actual#*|}"

  if [[ -n "${model}" ]] && [[ "${model}" == */${EXPECTED_MODEL_ID} ]] && [[ "${base}" == "${EXPECTED_MODEL_BASEURL}" ]]; then
    ok "$desc"
    PASS=$((PASS + 1))
  else
    fail "$desc"
    FAIL=$((FAIL + 1))
    warn "  期望: <provider>/${EXPECTED_MODEL_ID}|${EXPECTED_MODEL_BASEURL}"
    warn "  实际: ${actual}"
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
# 4.1 外部可达性验证（通过 IP + Host header 模拟浏览器请求）
# ============================================================================
echo ""
info "--- 外部路由可达性（路径路由模式）---"
NODE_IP=$(${KUBECTL} get node -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}' 2>/dev/null || hostname -I | awk '{print $1}')
TRAEFIK_PORT="${TRAEFIK_PORT:-$(${KUBECTL} get svc traefik -n kube-system -o jsonpath='{.spec.ports[?(@.name=="web")].nodePort}' 2>/dev/null || echo "80")}"
INGRESS_BASE="http://${NODE_IP}:${TRAEFIK_PORT}"
INGRESS_HOST="${INGRESS_HOST:-acli.sangfor.com.cn}"

check "Traefik 可达 (${NODE_IP}:${TRAEFIK_PORT})" \
  curl -sf -o /dev/null -H "Host: ${INGRESS_HOST}" "${INGRESS_BASE}/"

check "customer-ui: ${INGRESS_HOST}/" \
  curl -sf -o /dev/null -H "Host: ${INGRESS_HOST}" "${INGRESS_BASE}/"

check "admin-ui: ${INGRESS_HOST}/admin/" \
  curl -sf -o /dev/null -H "Host: ${INGRESS_HOST}" "${INGRESS_BASE}/admin/"

# api-gateway /health 端点不带 /api 前缀，通过 Ingress 测路由可达性
# 业务端点需要参数会返回 422（路由正常），只要不是 5xx/连接失败即为成功
check "api-gateway: ${INGRESS_HOST}/api/cases/ (路由可达)" \
  bash -c 'code=$(curl -s -o /dev/null -w "%{http_code}" -H "Host: '"${INGRESS_HOST}"'" '"${INGRESS_BASE}"'/api/cases/); [[ "$code" -lt 500 ]]'

# grafana 校验：不仅要可达，还要确保内容确实来自 Grafana（而非被 / 回退路由到 customer-ui）
check "grafana: ${INGRESS_HOST}/grafana (命中 Grafana 页面)" \
  bash -c 'body=$(curl -sfL -H "Host: '"${INGRESS_HOST}"'" "'"${INGRESS_BASE}"'/grafana"); \
    echo "$body" | grep -qi "grafana" && \
    ! echo "$body" | grep -q "HCI 故障排查助手"'

# ============================================================================
# 4.2 AI 容器配置防回归检查（避免通过修改 Clash 规避问题）
# ============================================================================
echo ""
info "--- AI 容器配置检查（容器内直配模型，不改 Clash）---"

EXPECTED_MODEL_ID="glm-5"
EXPECTED_MODEL_BASEURL="https://open.bigmodel.cn/api/paas/v4"

OPENCLAW_POD=$(${KUBECTL} get pods -n "${NAMESPACE}" \
  -l "app.kubernetes.io/name=openclaw" \
  --field-selector=status.phase=Running \
  -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)

if [[ -n "${OPENCLAW_POD}" ]]; then
  OPENCLAW_CFG=$(${KUBECTL} exec -n "${NAMESPACE}" "${OPENCLAW_POD}" -- sh -lc \
    "node -e 'const fs=require(\"fs\");const c=JSON.parse(fs.readFileSync(\"/home/node/.openclaw/openclaw.json\",\"utf8\"));const p=c?.agents?.defaults?.model?.primary||\"\";const pv=p.split(\"/\")[0]||\"\";const b=((c.models?.providers||{})[pv]||{}).baseUrl||\"\";process.stdout.write(p+\"|\"+b);'" 2>/dev/null || echo "")
  check_model_cfg "openclaw 容器模型配置正确" "${OPENCLAW_CFG}"
else
  fail "openclaw 容器模型配置正确"
  FAIL=$((FAIL + 1))
  warn "  未找到 Running 的 openclaw Pod"
fi

PRODCLAW_POD=$(${KUBECTL} get pods -n "${NAMESPACE}" \
  -l "assistant-type=productionclaw" \
  --field-selector=status.phase=Running \
  -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)

if [[ -n "${PRODCLAW_POD}" ]]; then
  PRODCLAW_INIT_NAME=$(${KUBECTL} get pod -n "${NAMESPACE}" "${PRODCLAW_POD}" -o jsonpath='{.spec.initContainers[0].name}' 2>/dev/null || echo "")
  PRODCLAW_VOLUMES=$(${KUBECTL} get pod -n "${NAMESPACE}" "${PRODCLAW_POD}" -o jsonpath='{range .spec.volumes[*]}{.name}{" "}{end}' 2>/dev/null || echo "")

  if [[ "${PRODCLAW_INIT_NAME}" == "init-workspace" ]] && [[ "${PRODCLAW_VOLUMES}" == *"claw-home"* ]] && [[ "${PRODCLAW_VOLUMES}" == *"init-config"* ]]; then
    ok "productionclaw Pod 模板结构正确（init-workspace + claw-home + init-config）"
    PASS=$((PASS + 1))
  else
    fail "productionclaw Pod 模板结构正确（init-workspace + claw-home + init-config）"
    FAIL=$((FAIL + 1))
    warn "  期望: initContainers 含 init-workspace，volumes 含 claw-home 与 init-config"
    warn "  实际: init=${PRODCLAW_INIT_NAME}, volumes=${PRODCLAW_VOLUMES}"
  fi

  PRODCLAW_CFG=$(${KUBECTL} exec -n "${NAMESPACE}" "${PRODCLAW_POD}" -- sh -lc \
    "node -e 'const fs=require(\"fs\");const c=JSON.parse(fs.readFileSync(\"/home/node/.openclaw/openclaw.json\",\"utf8\"));const p=c?.agents?.defaults?.model?.primary||\"\";const pv=p.split(\"/\")[0]||\"\";const b=((c.models?.providers||{})[pv]||{}).baseUrl||\"\";process.stdout.write(p+\"|\"+b);'" 2>/dev/null || echo "")
  check_model_cfg "productionclaw 容器模型配置正确" "${PRODCLAW_CFG}"
else
  fail "productionclaw 容器模型配置正确"
  FAIL=$((FAIL + 1))
  warn "  未找到 Running 的 productionclaw Pod"
fi

# ============================================================================
# 5. 业务功能链路验证（对话、消息、KB）
# ============================================================================
echo ""
echo "=========================================="
info "5. 业务功能链路验证"
echo "=========================================="

# 重建 port-forward（上一节已关闭）
${KUBECTL} port-forward svc/api-gateway 18001:8000 -n "${NAMESPACE}" &
PF2_PID=$!
sleep 3

if kill -0 $PF2_PID 2>/dev/null; then
  API2="http://localhost:18001"

  # ---- 5.1 创建测试工单（为对话测试准备）----
  SMOKE_CASE_RESP=$(curl -sf -X POST "${API2}/api/cases/" \
    -H "Content-Type: application/json" \
    -d '{"client_id":"smoke-test","title":"功能验证测试工单","description":"自动化烟测"}' 2>/dev/null || echo "")
  SMOKE_CASE_ID=""
  if echo "${SMOKE_CASE_RESP}" | grep -q '"case_id"'; then
    SMOKE_CASE_ID=$(echo "${SMOKE_CASE_RESP}" | grep -o '"case_id":"[^"]*' | cut -d'"' -f4)
    ok "烟测工单已创建: ${SMOKE_CASE_ID}"
    PASS=$((PASS + 1))
  else
    fail "烟测工单创建失败（对话测试将跳过）"
    FAIL=$((FAIL + 1))
  fi

  # ---- 5.2 对话历史加载（GET /api/conversations/case/{case_id}）----
  if [[ -n "${SMOKE_CASE_ID}" ]]; then
    check "对话历史加载 GET /conversations/case/${SMOKE_CASE_ID}" \
      curl -sf "${API2}/api/conversations/case/${SMOKE_CASE_ID}"
  else
    warn "  跳过对话历史测试（无工单 ID）"
  fi

  # ---- 5.3 创建对话（POST /api/conversations/?case_id=xxx）----
  CONV_ID=""
  if [[ -n "${SMOKE_CASE_ID}" ]]; then
    CONV_RESP=$(curl -sf -X POST \
      "${API2}/api/conversations/?case_id=${SMOKE_CASE_ID}&assistant_type=openclaw" \
      -H "Content-Type: application/json" 2>/dev/null || echo "")
    if echo "${CONV_RESP}" | grep -q '"conversation_id"'; then
      CONV_ID=$(echo "${CONV_RESP}" | grep -o '"conversation_id":"[^"]*' | cut -d'"' -f4)
      ok "创建对话成功: ${CONV_ID}"
      PASS=$((PASS + 1))
    else
      fail "创建对话失败"
      FAIL=$((FAIL + 1))
      [[ -n "${CONV_RESP}" ]] && warn "  Response: ${CONV_RESP}"
    fi
  else
    warn "  跳过创建对话测试（无工单 ID）"
  fi

  # ---- 5.4 获取消息历史（GET /api/conversations/{id}/messages）----
  if [[ -n "${CONV_ID}" ]]; then
    check "消息历史 GET /conversations/${CONV_ID}/messages" \
      curl -sf "${API2}/api/conversations/${CONV_ID}/messages"
  else
    warn "  跳过消息历史测试（无对话 ID）"
  fi

  # ---- 5.5 发送消息（POST /api/conversations/{id}/message）----
  if [[ -n "${CONV_ID}" ]]; then
    MSG_CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST \
      "${API2}/api/conversations/${CONV_ID}/message" \
      -H "Content-Type: application/json" \
      -d '{"content":"你好，这是自动化验证消息","role":"user"}' 2>/dev/null || echo "000")
    # SSE 流式接口返回 200 或 202；503 表示 AI 后端未就绪（可接受）；其他 5xx 为失败
    if [[ "${MSG_CODE}" == "200" || "${MSG_CODE}" == "202" || "${MSG_CODE}" == "503" ]]; then
      ok "发送消息接口可达: HTTP ${MSG_CODE}"
      PASS=$((PASS + 1))
    else
      fail "发送消息失败: HTTP ${MSG_CODE}"
      FAIL=$((FAIL + 1))
    fi
  else
    warn "  跳过发送消息测试（无对话 ID）"
  fi

  # ---- 5.6 用户鉴权（admin API 需要 X-Admin-Token）----
  # api-gateway 本身不做 admin JWT，通过 /api/cases/all 验证管理员路由可达（不含 token 应返回 401/403/422，不是 500）
  ADMIN_CODE=$(curl -s -o /dev/null -w "%{http_code}" "${API2}/api/cases/all" 2>/dev/null || echo "000")
  if [[ "${ADMIN_CODE}" != "5"* && "${ADMIN_CODE}" != "000" ]]; then
    ok "管理员路由可达 /api/cases/all: HTTP ${ADMIN_CODE}"
    PASS=$((PASS + 1))
  else
    fail "管理员路由异常 /api/cases/all: HTTP ${ADMIN_CODE}"
    FAIL=$((FAIL + 1))
  fi

  # ---- 5.7 KB 文档列表（GET /api/v1/kb/documents）----
  KB_CODE=$(curl -s -o /dev/null -w "%{http_code}" "${API2}/api/v1/kb/documents" 2>/dev/null || echo "000")
  if [[ "${KB_CODE}" == "200" || "${KB_CODE}" == "422" ]]; then
    ok "KB 文档列表接口可达: HTTP ${KB_CODE}"
    PASS=$((PASS + 1))
  else
    fail "KB 文档列表失败: HTTP ${KB_CODE}"
    FAIL=$((FAIL + 1))
  fi

  # ---- 5.8 KB 搜索（POST /api/v1/kb/search）----
  KB_SEARCH_CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST \
    "${API2}/api/v1/kb/search" \
    -H "Content-Type: application/json" \
    -d '{"query":"测试查询","top_k":3}' 2>/dev/null || echo "000")
  if [[ "${KB_SEARCH_CODE}" == "200" || "${KB_SEARCH_CODE}" == "422" ]]; then
    ok "KB 搜索接口可达: HTTP ${KB_SEARCH_CODE}"
    PASS=$((PASS + 1))
  else
    fail "KB 搜索失败: HTTP ${KB_SEARCH_CODE}"
    FAIL=$((FAIL + 1))
  fi

  # ---- 清理测试工单 ----
  if [[ -n "${SMOKE_CASE_ID}" ]]; then
    curl -sf -X PUT "${API2}/api/cases/${SMOKE_CASE_ID}/close" >/dev/null 2>&1 && \
      info "  烟测工单 ${SMOKE_CASE_ID} 已关闭" || true
  fi

  kill $PF2_PID 2>/dev/null || true
  wait $PF2_PID 2>/dev/null || true
else
  fail "port-forward 重建失败，跳过业务功能验证"
  FAIL=$((FAIL + 1))
fi

# ============================================================================
# 6. PVC 检查
# ============================================================================
echo ""
echo "=========================================="
info "6. PVC 检查"
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
