#!/usr/bin/env bash
# scripts/ci/check-hardcoded-namespace.sh
# D-3：检查 Helm templates 目录中是否存在硬编码命名空间字符串。
# 所有命名空间引用必须通过 {{ include "hci.namespace" . }} 或 .Release.Namespace。
#
# 用法：
#   bash scripts/ci/check-hardcoded-namespace.sh
#   bash scripts/ci/check-hardcoded-namespace.sh --chart deploy/helm/hci-platform
#
# 退出码：
#   0 — 无硬编码命名空间
#   1 — 发现硬编码命名空间

set -euo pipefail

# 默认扫描目录
CHARTS=(
  "deploy/helm/hci-platform"
  "deploy/helm/hci-platform-data"
  "deploy/helm/hci-platform-infra"
  "deploy/helm/hci-platform-obs"
)

# 解析参数
if [[ "${1:-}" == "--chart" && -n "${2:-}" ]]; then
  CHARTS=("$2")
fi

FOUND=0

for CHART in "${CHARTS[@]}"; do
  TEMPLATES_DIR="${CHART}/templates"
  if [[ ! -d "${TEMPLATES_DIR}" ]]; then
    continue
  fi

  # 查找硬编码命名空间，排除 _helpers.tpl 和行内注释
  RESULT=$(grep -rn \
    --include="*.yaml" \
    -E "namespace:\s+\"?(hci-troubleshoot|hci-dev|hci-staging|hci-prod)\"?" \
    "${TEMPLATES_DIR}/" \
    | grep -v "_helpers.tpl" \
    | grep -v "^\s*#" \
    || true)

  if [[ -n "${RESULT}" ]]; then
    echo "[ERROR] ${CHART}: 发现硬编码命名空间，请改用 {{ include \"hci.namespace\" . }} 或 .Release.Namespace："
    echo "${RESULT}"
    FOUND=1
  fi
done

if [[ "${FOUND}" -eq 0 ]]; then
  echo "[OK] check-hardcoded-namespace: 所有命名空间引用均为模板化（扫描 ${#CHARTS[@]} 个 Chart）"
  exit 0
else
  exit 1
fi
