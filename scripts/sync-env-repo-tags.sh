#!/usr/bin/env bash
# 将镜像 tag 同步到环境仓库 values.yaml（按环境/服务维度）

set -euo pipefail

ENV_REPO_PATH="${ENV_REPO_PATH:-}"
TARGET_ENV="${TARGET_ENV:-dev}"
IMAGE_TAG="${IMAGE_TAG:-}"
SERVICES_CSV="${SERVICES_CSV:-apiGateway,caseService,conversationService,schedulerService,kbService,customerUI,adminUI}"

if [[ -z "$ENV_REPO_PATH" ]]; then
  echo "ENV_REPO_PATH 未设置"
  echo "推荐固定路径: /mnt/d/aihci/hci-platform-env"
  echo "示例:"
  echo "  git clone git@github.com:<your-org>/hci-platform-env.git /mnt/d/aihci/hci-platform-env"
  echo "  ENV_REPO_PATH=/mnt/d/aihci/hci-platform-env TARGET_ENV=dev IMAGE_TAG=2026.03.19-smoke bash scripts/sync-env-repo-tags.sh"
  exit 1
fi

if [[ -z "$IMAGE_TAG" ]]; then
  echo "IMAGE_TAG 未设置"
  exit 1
fi

VALUES_FILE="${ENV_REPO_PATH}/environments/${TARGET_ENV}/values.yaml"
if [[ ! -f "$VALUES_FILE" ]]; then
  echo "未找到目标 values 文件: $VALUES_FILE"
  exit 1
fi

update_service_tag() {
  local file="$1"
  local service_key="$2"
  local tag="$3"
  local tmp

  tmp="$(mktemp)"
  awk -v key="$service_key" -v tag="$tag" '
    BEGIN { in_block=0 }
    {
      if ($0 ~ "^" key ":$") { in_block=1; print; next }
      if (in_block && $0 ~ /^[A-Za-z0-9_]+:$/ && $0 !~ /^image:$/) { in_block=0 }
      if (in_block && $0 ~ /^[[:space:]]+tag:[[:space:]]*"[^"]*"/) {
        sub(/tag:[[:space:]]*"[^"]*"/, "tag: \"" tag "\"")
      }
      print
    }
  ' "$file" > "$tmp"
  mv "$tmp" "$file"
}

IFS=',' read -r -a services <<< "$SERVICES_CSV"
for svc in "${services[@]}"; do
  echo "更新 ${svc}.image.tag -> ${IMAGE_TAG}"
  update_service_tag "$VALUES_FILE" "$svc" "$IMAGE_TAG"
done

echo "同步完成: ${VALUES_FILE}"
