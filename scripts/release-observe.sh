#!/usr/bin/env bash
# 发布后观察脚本（建议观察窗口 30 分钟）
# 用法：
#   NAMESPACE=hci RELEASE_NAME=hci-platform bash scripts/release-observe.sh

set -euo pipefail

NAMESPACE="${NAMESPACE:-hci}"
RELEASE_NAME="${RELEASE_NAME:-hci-platform}"
WINDOW_MINUTES="${WINDOW_MINUTES:-30}"
SAMPLE_INTERVAL_SECONDS="${SAMPLE_INTERVAL_SECONDS:-60}"
OUTPUT_DIR="${OUTPUT_DIR:-./.local/release-observe}"

mkdir -p "$OUTPUT_DIR"
TS="$(date +%Y%m%d-%H%M%S)"
REPORT_FILE="${OUTPUT_DIR}/observe-${NAMESPACE}-${RELEASE_NAME}-${TS}.log"

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

if ! helm -n "$NAMESPACE" status "$RELEASE_NAME" >/dev/null 2>&1; then
  log "未找到 release: ${RELEASE_NAME}（namespace=${NAMESPACE}）"
  log "请先确认命名空间和 release 名称，或先完成部署后再执行发布观察。"
  exit 1
fi

TOTAL_SECONDS=$((WINDOW_MINUTES * 60))
if [[ "$SAMPLE_INTERVAL_SECONDS" -le 0 ]]; then
  log "SAMPLE_INTERVAL_SECONDS 必须大于 0"
  exit 1
fi

LOOPS=$((TOTAL_SECONDS / SAMPLE_INTERVAL_SECONDS))
if [[ "$LOOPS" -le 0 ]]; then
  LOOPS=1
fi

log "开始发布后观察"
log "namespace=${NAMESPACE}, release=${RELEASE_NAME}, window=${WINDOW_MINUTES}m, interval=${SAMPLE_INTERVAL_SECONDS}s"

log "=== 基线快照 ==="
kubectl -n "$NAMESPACE" get deploy | tee -a "$REPORT_FILE"
kubectl -n "$NAMESPACE" get pods -o wide | tee -a "$REPORT_FILE"
helm -n "$NAMESPACE" history "$RELEASE_NAME" | tail -n 5 | tee -a "$REPORT_FILE"

for ((i=1; i<=LOOPS; i++)); do
  log "=== 采样 ${i}/${LOOPS} ==="
  kubectl -n "$NAMESPACE" get pods --no-headers | tee -a "$REPORT_FILE"

  # 采样最近 5 分钟 Warning 事件，快速识别异常
  kubectl -n "$NAMESPACE" get events \
    --field-selector type=Warning \
    --sort-by=.metadata.creationTimestamp | tail -n 20 | tee -a "$REPORT_FILE" || true

  if [[ "$i" -lt "$LOOPS" ]]; then
    sleep "$SAMPLE_INTERVAL_SECONDS"
  fi
done

log "=== 结束快照 ==="
kubectl -n "$NAMESPACE" get deploy | tee -a "$REPORT_FILE"
kubectl -n "$NAMESPACE" get pods -o wide | tee -a "$REPORT_FILE"

log "观察完成，报告输出: ${REPORT_FILE}"
