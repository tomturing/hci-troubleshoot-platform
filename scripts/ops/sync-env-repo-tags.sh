#!/usr/bin/env bash
# =============================================================================
# 🟢 运维脚本 — 期展镜像 Tag 到环境仓库
# =============================================================================
# 职责：将镜像 Tag 同步写入环境仓库的 values.yaml（按环境/服务维度）
# 使用场景：CI 流水线中自动调用（人工一般不直接运行）
# 使用方法：
#   ENV_REPO_PATH=/path/to/env-repo TARGET_ENV=dev IMAGE_TAG=20260319-1430-abc1234 \
#     SERVICES_CSV=apiGateway,caseService bash scripts/ops/sync-env-repo-tags.sh
# 影响范围：🟡 第三方环境仓库（hci-platform-env）的 values.yaml
# =============================================================================

ENV_REPO_PATH="${ENV_REPO_PATH:-}"
TARGET_ENV="${TARGET_ENV:-dev}"
IMAGE_TAG="${IMAGE_TAG:-}"
SERVICES_CSV="${SERVICES_CSV:-apiGateway,caseService,conversationService,schedulerService,kbService,customerUI,adminUI}"
# 镜像仓库前缀（与 values.yaml 中的 global.imageRegistry 一致）
IMAGE_REGISTRY="${IMAGE_REGISTRY:-ghcr.io/tomturing/hci-troubleshoot-platform}"

# 禁止被本脚本更新的 key（来自独立仓库，有自己的发布流程）
BLOCKED_SERVICES="opsAgent"

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
  # 跳过来自独立仓库的服务，防止 tag 被 htp CI 错误覆盖
  if echo ",${BLOCKED_SERVICES}," | grep -q ",${svc},"; then
    echo "⚠️  跳过 ${svc}（在保护名单中，tag 来自独立仓库）"
    continue
  fi
  echo "更新 ${svc}.image.tag -> ${IMAGE_TAG}"
  update_service_tag "$VALUES_FILE" "$svc" "$IMAGE_TAG"
done

# dbMigrate.image 是完整镜像 URL（非 .image.tag 嵌套结构），单独处理
update_db_migrate_image() {
  local file="$1"
  local tag="$2"
  local new_image="${IMAGE_REGISTRY}/db-migrate:${tag}"
  local tmp
  tmp="$(mktemp)"
  # 匹配 dbMigrate: 块内的 image: "..." 行，整体替换 URL
  awk -v new_img="$new_image" '
    BEGIN { in_block=0 }
    {
      if ($0 ~ /^dbMigrate:$/) { in_block=1; print; next }
      if (in_block && $0 ~ /^[A-Za-z0-9_]+:$/) { in_block=0 }
      if (in_block && $0 ~ /^[[:space:]]+image:[[:space:]]*"[^"]*"/) {
        sub(/image:[[:space:]]*"[^"]*"/, "image: \"" new_img "\"")
      }
      print
    }
  ' "$file" > "$tmp"
  mv "$tmp" "$file"
}

echo "更新 dbMigrate.image -> ${IMAGE_REGISTRY}/db-migrate:${IMAGE_TAG}"
update_db_migrate_image "$VALUES_FILE" "$IMAGE_TAG"

echo "同步完成: ${VALUES_FILE}"
