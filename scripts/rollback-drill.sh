#!/usr/bin/env bash
# 回滚演练脚本
# 默认仅演练，不执行真实回滚。
# 用法：
#   NAMESPACE=hci RELEASE_NAME=hci-platform bash scripts/rollback-drill.sh
#   NAMESPACE=hci RELEASE_NAME=hci-platform EXECUTE=true TARGET_REVISION=12 bash scripts/rollback-drill.sh

set -euo pipefail

NAMESPACE="${NAMESPACE:-hci}"
RELEASE_NAME="${RELEASE_NAME:-hci-platform}"
EXECUTE="${EXECUTE:-false}"
TARGET_REVISION="${TARGET_REVISION:-}"
OUTPUT_DIR="${OUTPUT_DIR:-./.local/rollback-drill}"

mkdir -p "$OUTPUT_DIR"
TS="$(date +%Y%m%d-%H%M%S)"
REPORT_FILE="${OUTPUT_DIR}/rollback-${NAMESPACE}-${RELEASE_NAME}-${TS}.log"

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$REPORT_FILE"
}

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    log "缺少命令: $1"
    exit 1
  fi
}

require_cmd kubectl
require_cmd helm

log "开始回滚演练"
log "namespace=${NAMESPACE}, release=${RELEASE_NAME}, execute=${EXECUTE}"

if ! helm -n "$NAMESPACE" status "$RELEASE_NAME" >/dev/null 2>&1; then
  log "未找到 release: ${RELEASE_NAME}（namespace=${NAMESPACE}）"
  log "请先确认命名空间和 release 名称，或先执行一次部署后再进行回滚演练。"
  exit 1
fi

log "=== 发布历史 ==="
helm -n "$NAMESPACE" history "$RELEASE_NAME" | tee -a "$REPORT_FILE"

if [[ -z "$TARGET_REVISION" ]]; then
  TARGET_REVISION="$(helm -n "$NAMESPACE" history "$RELEASE_NAME" -o json | sed -n 's/.*"revision":\([0-9][0-9]*\).*/\1/p' | tail -n 2 | head -n 1)"
fi

if [[ -z "$TARGET_REVISION" ]]; then
  log "无法自动推导 TARGET_REVISION，请手动指定"
  exit 1
fi

log "目标回滚 revision=${TARGET_REVISION}"

if [[ "$EXECUTE" != "true" ]]; then
  log "当前为演练模式，不执行真实回滚。"
  log "如需执行，请设置 EXECUTE=true。"
  log "演练报告输出: ${REPORT_FILE}"
  exit 0
fi

log "执行真实回滚..."
helm -n "$NAMESPACE" rollback "$RELEASE_NAME" "$TARGET_REVISION" | tee -a "$REPORT_FILE"

log "=== 回滚后状态 ==="
kubectl -n "$NAMESPACE" get deploy | tee -a "$REPORT_FILE"
kubectl -n "$NAMESPACE" get pods -o wide | tee -a "$REPORT_FILE"

log "真实回滚执行完成，报告输出: ${REPORT_FILE}"
