#!/usr/bin/env bash
# ============================================================================
# diagnosis-service 签名/加密密钥 Secret 幂等引导
# ============================================================================
# 用途：集群重建或新环境初始化时，按 GitOps 声明恢复
#       diagnosisService.existingCredentialsSecret 指向的 Secret。
#
# 设计约束（第一性原理）：
#   - 私钥禁止入仓，所以 GitOps 只声明「契约」不声明「密钥材料」；
#   - 密钥必须稳定可恢复：自动生成随机密钥会让历史采集签名无法验证、
#     已加密诊断数据永久不可解，故本脚本绝不生成密钥；
#   - 幂等且保护存量：Secret 已存在且四个键齐全时原样不动，不覆盖、不轮换。
#
# 唯一调用链：每次执行生成 bootstrap-signing-<时间戳>-<pid>，全部日志带该
# trace_id，便于把「密钥注入 → diagnosis-service 启动 → 首次验签」串起来。
#
# 用法：
#   bash scripts/ops/bootstrap-diagnosis-signing-secret.sh \
#       --keys-dir /secure/path/diagnosis-keys [--namespace hci-dev]
#
# 密钥目录需包含以下 4 个同名文件，文件内容即该键的原始值：
#   COLLECTOR_SIGNING_PRIVATE_KEY_B64      32 字节原始 Ed25519 私钥的 base64 文本
#   COLLECTOR_SIGNING_KEY_ID               签名公钥 ID
#   DIAGNOSIS_ENCRYPTION_PRIVATE_KEY_B64   PEM RSA-3072 私钥的 base64 文本
#   DIAGNOSIS_ENCRYPTION_KEY_ID            加密公钥 ID
# ============================================================================

set -euo pipefail

NAMESPACE="${NAMESPACE:-hci-dev}"
SECRET_NAME="${SECRET_NAME:-diagnosis-service-signing}"
KEYS_DIR=""
TRACE_ID="bootstrap-signing-$(date +%Y%m%d%H%M%S)-$$"

REQUIRED_KEYS=(
  COLLECTOR_SIGNING_PRIVATE_KEY_B64
  COLLECTOR_SIGNING_KEY_ID
  DIAGNOSIS_ENCRYPTION_PRIVATE_KEY_B64
  DIAGNOSIS_ENCRYPTION_KEY_ID
)

log() {
  printf '[trace_id=%s] %s\n' "$TRACE_ID" "$*"
}

usage() {
  cat <<'USAGE'
用法: bootstrap-diagnosis-signing-secret.sh --keys-dir <目录> [--namespace <命名空间>] [--secret-name <名称>]

--keys-dir 必须指向受控密钥源，且包含四个同名文件，内容为对应键的原始文本。
脚本不会生成密钥；Secret 已存在且四键齐全时直接退出 0，不改动任何数据。
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --keys-dir)    KEYS_DIR="$2"; shift 2 ;;
    --namespace)   NAMESPACE="$2"; shift 2 ;;
    --secret-name) SECRET_NAME="$2"; shift 2 ;;
    -h|--help)     usage; exit 0 ;;
    *) log "❌ 未知参数: $1"; usage; exit 2 ;;
  esac
done

log "检查 ${NAMESPACE}/${SECRET_NAME} 现状"

# 幂等分支：已存在且四键齐全则保持原样。覆盖存量密钥等于让历史数据不可解，
# 因此即使某个键缺失也只报错，不自动修补。
if kubectl -n "$NAMESPACE" get secret "$SECRET_NAME" -o name >/dev/null 2>&1; then
  MISSING=0
  for KEY in "${REQUIRED_KEYS[@]}"; do
    if ! kubectl -n "$NAMESPACE" get secret "$SECRET_NAME" \
        -o jsonpath="{.data.${KEY}}" 2>/dev/null | grep -q .; then
      log "⚠️ 现有 Secret 缺少键: ${KEY}"
      MISSING=$((MISSING + 1))
    fi
  done
  if [ "$MISSING" -eq 0 ]; then
    log "✅ Secret 已存在且四键齐全，保持原样（不轮换密钥）"
    exit 0
  fi
  log "❌ 现有 Secret 不完整；为避免覆盖存量密钥，请人工核对后处理"
  exit 1
fi

if [ -z "$KEYS_DIR" ]; then
  log "❌ Secret 不存在，必须通过 --keys-dir 指定受控密钥源"
  usage
  exit 2
fi

for KEY in "${REQUIRED_KEYS[@]}"; do
  if [ ! -s "${KEYS_DIR}/${KEY}" ]; then
    log "❌ 密钥文件缺失或为空: ${KEYS_DIR}/${KEY}"
    exit 2
  fi
done

FROM_FILE_ARGS=()
for KEY in "${REQUIRED_KEYS[@]}"; do
  FROM_FILE_ARGS+=(--from-file "${KEY}=${KEYS_DIR}/${KEY}")
done

kubectl -n "$NAMESPACE" create secret generic "$SECRET_NAME" "${FROM_FILE_ARGS[@]}" \
  --dry-run=client -o yaml | kubectl apply -f -

log "✅ 已按受控密钥源创建 ${NAMESPACE}/${SECRET_NAME}（未生成任何新密钥）"
