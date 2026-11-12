#!/usr/bin/env bash
# ============================================================================
# dev 环境能力面依赖 Secret 幂等引导
# ============================================================================
# 用途：把「staging 有、dev 没有」的运维 provision 补齐，使 GitOps 声明的
#       hci-sim 与 DSH 在 dev 能真正启动，而不是靠手工 Secret 或 dev-local
#       镜像漂移撑着。
#
# 第一性原理约束：
#   - GitOps 只声明契约，不声明密钥材料；本脚本不生成任何密钥，只从集群内
#     既有受控 Secret 复制/派生，绝不把值写入仓库、日志或命令行回显。
#   - 幂等且保护存量：目标键已存在时保持原样；补键用 merge patch，不重建
#     Secret，避免抹掉 lease-hmac-key / ssh_host_key 等历史状态。
#   - DSH 的 LLM baseURL 是内部 oneapi，与平台通用 llm.baseUrl 不同源；
#     未提供专用凭证时只能用通用 key 满足「进程可启动」这一层契约，
#     分析调用仍需 --dsh-api-key-file provision 专用凭证。
#
# 唯一调用链：每次执行生成 bootstrap-dev-parity-<时间戳>-<pid>，所有日志
# 带该 trace_id，可把「凭据注入 → Pod 启动 → 冒烟验收」串成完整链路。
#
# 用法：
#   bash scripts/ops/bootstrap-dev-parity-credentials.sh \
#       [--dsh-api-key-file /secure/path/dsh_qwen_key] [--pg-host-port host:5432]
# ============================================================================

set -euo pipefail

DEV_NS="${DEV_NS:-hci-dev}"
SIM_NS="${SIM_NS:-hci-sim-dev}"
DSH_NS="${DSH_NS:-hci-dsh}"
PG_HOSTPORT="${PG_HOSTPORT:-postgres.hci-dev.svc.cluster.local:5432}"
SIM_DB_USER="${SIM_DB_USER:-hci_sim_runtime}"
DSH_KEY_FILE=""
TRACE_ID="bootstrap-dev-parity-$(date +%Y%m%d%H%M%S)-$$"

log() {
  printf '[trace_id=%s] %s\n' "$TRACE_ID" "$*" >&2
}

usage() {
  cat <<'USAGE'
用法: bootstrap-dev-parity-credentials.sh [--dsh-api-key-file <文件>] [--pg-host-port <host:port>]

--dsh-api-key-file  可选，指向受控的 DSH 专用 LLM 凭证文件（内容即 key 原文）。
                    未提供时回退到 hci-dev/hci-secrets.LLM_API_KEY 作为启动占位。
--pg-host-port      可选，hci_sim 数据库地址，默认 postgres.hci-dev.svc.cluster.local:5432。
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dsh-api-key-file) DSH_KEY_FILE="$2"; shift 2 ;;
    --pg-host-port)     PG_HOSTPORT="$2"; shift 2 ;;
    -h|--help)          usage; exit 0 ;;
    *) log "❌ 未知参数: $1"; usage; exit 2 ;;
  esac
done

# 读取既有受控 Secret 的单个键；失败即终止，绝不静默使用空值。
read_secret() {
  local ns="$1" name="$2" key="$3" raw
  if ! kubectl -n "$ns" get secret "$name" -o name >/dev/null 2>&1; then
    log "❌ 依赖 Secret 不存在: ${ns}/${name}"
    return 1
  fi
  raw="$(kubectl -n "$ns" get secret "$name" -o jsonpath="{.data.${key}}" 2>/dev/null || true)"
  if [ -z "$raw" ]; then
    log "❌ Secret 键缺失或为空: ${ns}/${name}.${key}"
    return 1
  fi
  printf '%s' "$raw" | base64 -d
}

has_key() {
  local ns="$1" name="$2" key="$3"
  [ -n "$(kubectl -n "$ns" get secret "$name" -o jsonpath="{.data.${key}}" 2>/dev/null || true)" ]
}

ensure_key() {
  local ns="$1" name="$2" key="$3" value="$4" b64
  if kubectl -n "$ns" get secret "$name" -o name >/dev/null 2>&1; then
    if has_key "$ns" "$name" "$key"; then
      log "✅ ${ns}/${name}.${key} 已存在，保持原样"
      return 0
    fi
    b64="$(printf '%s' "$value" | base64 -w0)"
    kubectl -n "$ns" patch secret "$name" --type merge \
      -p "{\"data\":{\"${key}\":\"${b64}\"}}" >/dev/null
    log "✅ 补齐 ${ns}/${name}.${key}（merge patch，保留其它键）"
  else
    kubectl -n "$ns" create secret generic "$name" \
      --from-literal="${key}=${value}" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
    log "✅ 创建 ${ns}/${name}.${key}"
  fi
}

log "开始 dev 能力面依赖引导（不生成任何密钥材料）"

# 1) DSH 独立命名空间：chart 也会渲染该 Namespace，apply 幂等不冲突。
kubectl create namespace "$DSH_NS" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
log "✅ 命名空间 ${DSH_NS} 就绪"

# 2) hci-sim Runtime 数据库 URL。scheme 必须为 postgres/postgresql，
#    path 必须精确为 /hci_sim（见 hci_sim/internal/database/config.go）。
RUNTIME_PW="$(read_secret "$DEV_NS" hci-sim-db-credentials RUNTIME_PASSWORD)"
ENCODED_PW="$(python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$RUNTIME_PW")"
DB_URL="postgresql://${SIM_DB_USER}:${ENCODED_PW}@${PG_HOSTPORT}/hci_sim"
ensure_key "$SIM_NS" hci-sim-runtime-credentials HCI_SIM_DATABASE_URL "$DB_URL"

# 3) 控制面共享令牌：Runtime 与网关必须读同一 canonical key，避免 403 漂移。
INTERNAL_TOKEN="$(read_secret "$DEV_NS" hci-secrets INTERNAL_API_TOKEN)"
ensure_key "$SIM_NS" hci-sim-credentials INTERNAL_API_TOKEN "$INTERNAL_TOKEN"

# 4) DSH LLM 凭证：chart 的 envFrom 无条件引用 dsh-llm-credentials，
#    而渲染条件依赖 secrets.dshLlmApiKey（dev 为空），属于运维 provision 契约。
if [ -n "$DSH_KEY_FILE" ]; then
  if [ ! -s "$DSH_KEY_FILE" ]; then
    log "❌ DSH 凭证文件缺失或为空: ${DSH_KEY_FILE}"
    exit 2
  fi
  DSH_KEY="$(cat "$DSH_KEY_FILE")"
  log "DSH LLM 凭证来源：受控密钥文件"
else
  DSH_KEY="$(read_secret "$DEV_NS" hci-secrets LLM_API_KEY)"
  log "⚠️ 未提供 --dsh-api-key-file，使用平台通用 LLM_API_KEY 作为启动占位"
  log "   DSH 分析走内部 oneapi，需另行 provision 专用凭证才能执行真实诊断"
fi
ensure_key "$DSH_NS" dsh-llm-credentials DSH_QWEN_API_KEY "$DSH_KEY"

# 5) 插件凭据 Secret：dev 使用离线 bundle，允许空 Secret 满足 envFrom 契约。
if kubectl -n "$DSH_NS" get secret dsh-plugin-credentials -o name >/dev/null 2>&1; then
  log "✅ ${DSH_NS}/dsh-plugin-credentials 已存在，保持原样"
else
  kubectl -n "$DSH_NS" create secret generic dsh-plugin-credentials \
    --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  log "✅ 创建 ${DSH_NS}/dsh-plugin-credentials（空占位）"
fi

# 6) 环境角色标签：commit/PR 标识的 env 数据来源，缺失会让环境无法判定。
if [ "$(kubectl get ns argocd -o jsonpath='{.metadata.labels.hci\.env\.role}' 2>/dev/null || true)" != "dev" ]; then
  kubectl label ns argocd hci.env.role=dev --overwrite >/dev/null
  log "✅ 补齐 argocd 命名空间 hci.env.role=dev 标签"
else
  log "✅ argocd 命名空间环境标签已就绪"
fi

log "✅ dev 能力面依赖引导完成；下一步 apply root app 并执行冒烟门禁"
