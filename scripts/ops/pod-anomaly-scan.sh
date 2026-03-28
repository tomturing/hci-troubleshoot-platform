#!/usr/bin/env bash
set -euo pipefail

KUBECTL="${KUBECTL:-kubectl}"
TRACE_ID="hci-pod-scan-$(date +%Y%m%d%H%M%S)-$$"

info() {
  echo "[INFO][${TRACE_ID}] $*"
}

section() {
  echo
  echo "==================== $* ===================="
}

section "异常 Pod（非 Running/Succeeded）"
${KUBECTL} get pods -A --field-selector=status.phase!=Running,status.phase!=Succeeded -o wide || true

section "常见等待原因聚合"
${KUBECTL} get pods -A -o jsonpath='{range .items[*]}{.metadata.namespace}{"/"}{.metadata.name}{"|"}{range .status.initContainerStatuses[*]}init:{.state.waiting.reason}{","}{end}{range .status.containerStatuses[*]}container:{.state.waiting.reason}{","}{end}{"\n"}{end}' \
  | grep -E 'ImagePullBackOff|ErrImagePull|CrashLoopBackOff|CreateContainerError|RunContainerError|ContainerCreating|Init:ImagePullBackOff' || true

section "最近事件（全局）"
${KUBECTL} get events -A --sort-by=.lastTimestamp | tail -n 120 || true

section "建议优先处理"
echo "1) 先处理 ImagePullBackOff：检查镜像仓库连通性、镜像 tag 与 imagePullSecrets。"
echo "2) 对 latest 镜像改为不可变 tag，避免重建时拉取漂移。"
echo "3) 对 CronJob/Job 设置 activeDeadlineSeconds、backoffLimit、ttlSecondsAfterFinished。"

info "扫描完成"
