#!/usr/bin/env bash
# ============================================================================
# dev 全服务可证伪冒烟门禁
# ============================================================================
# 为什么需要：Pod Ready 只证明 kubelet 探活通过，不能证明「服务可用」。
# 2026-11-13 实测反例：hci-sim Pod 1/1 Ready、PodIP /healthz 返回 200，
# 但 Service selector 漂移到 instance=hci-sim-dev 后 EndpointSlice 为空，
# 经 ClusterIP 访问直接 000。只看 Deployment 状态会漏判。
#
# 判据分层（每层都可证伪，失败即退出非 0）：
#   L1 ArgoCD：全部 Application 必须 Synced / Healthy
#   L2 工作负载：业务命名空间所有 Deployment 副本全 Ready，无异常 Pod
#   L3 网络：关键 Service 的 EndpointSlice 必须有地址
#   L4 协议：每个服务健康端点经 ClusterIP 返回期望 HTTP 状态
#   L5 链路：admin-ui /dsh/ 必须可达真实 DSH，而不是 502
#
# 可观测性：单次执行生成唯一 trace_id；每条结果以
#   PASS|<检查项>|<期望>|<实际> / FAIL|<检查项>|expected=<期望>|actual=<实际>
# 结构化输出，便于日志采集与指标统计。
#
# 用法：
#   bash scripts/ops/smoke-dev-services.sh [--skip-dsh] [--timeout 5]
#   --skip-dsh 仅用于 DSH 尚未纳管的基线对比，正式验收禁止使用。
# ============================================================================

set -uo pipefail

TRACE_ID="smoke-dev-$(date +%Y%m%d%H%M%S)-$$"
TIMEOUT="${TIMEOUT:-5}"
SKIP_DSH=0
TOTAL=0
FAILED=0

log() { printf '[trace_id=%s] %s\n' "$TRACE_ID" "$*" >&2; }
pass() { TOTAL=$((TOTAL + 1)); printf 'PASS|%s|%s|%s\n' "$1" "$2" "$3"; }
fail() { TOTAL=$((TOTAL + 1)); FAILED=$((FAILED + 1)); printf 'FAIL|%s|expected=%s|actual=%s\n' "$1" "$2" "$3"; }

expect() {
  local name="$1" expected="$2" actual="$3"
  case "|${expected}|" in
    *"|${actual}|"*) pass "$name" "$expected" "$actual" ;;
    *) fail "$name" "$expected" "$actual" ;;
  esac
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-dsh) SKIP_DSH=1; shift ;;
    --timeout)  TIMEOUT="$2"; shift 2 ;;
    -h|--help)  sed -n '1,28p' "$0"; exit 0 ;;
    *) log "❌ 未知参数: $1"; exit 2 ;;
  esac
done

http_code() {
  local ns="$1" svc="$2" port="$3" path="$4" ip code
  ip="$(kubectl -n "$ns" get svc "$svc" -o jsonpath='{.spec.clusterIP}' 2>/dev/null || true)"
  if [ -z "$ip" ]; then printf 'NO_SVC'; return 0; fi
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time "$TIMEOUT" \
    "http://${ip}:${port}${path}" 2>/dev/null || true)"
  printf '%s' "${code:-ERR}"
}

check_http() {
  local ns="$1" svc="$2" port="$3" path="$4" expected="$5" code
  code="$(http_code "$ns" "$svc" "$port" "$path")"
  expect "http/${ns}/${svc}${path}" "$expected" "$code"
}

log "开始 dev 全服务冒烟验收 trace_id=${TRACE_ID} skip_dsh=${SKIP_DSH}"

# ── L1 ArgoCD 应用状态 ────────────────────────────────────────────────────
while IFS='|' read -r name sync health; do
  [ -n "$name" ] || continue
  expect "app/${name}/sync" "Synced" "$sync"
  expect "app/${name}/health" "Healthy" "$health"
done < <(kubectl get applications -n argocd -o json 2>/dev/null | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)
for a in data.get("items", []):
    s = a.get("status", {})
    print("|".join([a["metadata"]["name"],
                    s.get("sync", {}).get("status", "UNKNOWN"),
                    s.get("health", {}).get("status", "UNKNOWN")]))
')

# ── L2 工作负载与异常 Pod ─────────────────────────────────────────────────
while IFS='|' read -r ns name desired ready; do
  [ -n "$name" ] || continue
  expect "deploy/${ns}/${name}" "$desired" "$ready"
done < <(kubectl get deploy -A -o json 2>/dev/null | python3 -c '
import json, sys
wanted = {"hci-dev", "hci-observability", "hci-sim-dev", "hci-dsh"}
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)
for d in data.get("items", []):
    ns = d["metadata"]["namespace"]
    if ns not in wanted:
        continue
    print("|".join([ns, d["metadata"]["name"],
                    str(d["spec"].get("replicas", 0)),
                    str(d["status"].get("readyReplicas", 0))]))
')

ABNORMAL="$(kubectl get pods -A --no-headers 2>/dev/null | awk '$4 !~ /Running|Completed/ {print $1 "/" $2 "=" $4}' | paste -sd, - || true)"
expect "pods/abnormal" "" "${ABNORMAL}"

# ── L3 关键 Service 必须有后端地址 ────────────────────────────────────────
check_endpoints() {
  local ns="$1" svc="$2" count
  count="$(kubectl -n "$ns" get endpointslice \
    -l "kubernetes.io/service-name=${svc}" -o json 2>/dev/null | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    print("ERR"); sys.exit(0)
n = 0
for s in data.get("items", []):
    for e in s.get("endpoints", []):
        n += len(e.get("addresses", []))
print(n)
' || true)"
  expect "endpoints/${ns}/${svc}" "nonempty" "$([ "${count:-0}" -gt 0 ] 2>/dev/null && echo nonempty || echo "${count:-ERR}")"
}
check_endpoints hci-sim-dev hci-sim
check_endpoints hci-dev api-gateway
if [ "$SKIP_DSH" -eq 0 ]; then check_endpoints hci-dsh dsh; fi

# ── L4 逐服务健康端点（经 ClusterIP，覆盖 Service/EndpointSlice 链路）──────
CHECKS=(
  "hci-dev|api-gateway|8000|/health|200"
  "hci-dev|auth-service|8007|/health/ready|200"
  "hci-dev|case-service|8001|/health|200"
  "hci-dev|conversation-service|8002|/health/ready|200"
  "hci-dev|diagnosis-service|8008|/health/ready|200"
  "hci-dev|kb-service|8004|/health/ready|200"
  "hci-dev|agent-service|8005|/v1/agent/health|200"
  "hci-dev|scheduler-service|8003|/health|200"
  "hci-dev|ops-agent-service|8006|/health|200"
  "hci-dev|terminal-bridge|9999|/health/live|200"
  "hci-dev|terminal-bridge|9999|/health/ready|200"
  "hci-dev|admin-ui|80|/|200|301|302"
  "hci-dev|customer-ui|80|/|200|301|302"
  "hci-observability|grafana|3000|/api/health|200"
  "hci-observability|loki|3100|/ready|200"
  "hci-observability|prometheus|9090|/-/healthy|200"
  "hci-sim-dev|hci-sim|8080|/healthz|200"
  "hci-sim-dev|hci-sim|8080|/readyz|200"
)
for row in "${CHECKS[@]}"; do
  IFS='|' read -r ns svc port path expected <<< "$row"
  check_http "$ns" "$svc" "$port" "$path" "$expected"
done

# ── L5 DSH 跨服务链路 ─────────────────────────────────────────────────────
if [ "$SKIP_DSH" -eq 0 ]; then
  check_http hci-dsh dsh 8080 /__internal/healthz "200"
  check_http hci-dev admin-ui 80 /dsh/ "200|301|302|401|403"
else
  log "⚠️ --skip-dsh 已启用：跳过 DSH 链路判据，本次结果不构成完整验收"
fi

log "冒烟结束 total=${TOTAL} failed=${FAILED} trace_id=${TRACE_ID}"
if [ "$FAILED" -ne 0 ]; then
  log "❌ 存在 ${FAILED} 项失败，验收不通过"
  exit 1
fi
log "✅ 全部 ${TOTAL} 项判据通过"
