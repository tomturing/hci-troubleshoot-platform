#!/usr/bin/env bash
set -euo pipefail
# =============================================================================
# 🟢 运维脚本 — 同步镜像 Tag 到环境仓库
# =============================================================================
# 职责：将镜像 Tag 同步写入环境仓库的 values.yaml（按环境/服务维度）
# 使用场景：CI 流水线中自动调用（人工一般不直接运行）
# 使用方法：
#   ENV_REPO_PATH=/path/to/env-repo TARGET_ENV=dev IMAGE_TAG=20260319-1430-abc1234 \
#     SERVICES_CSV=apiGateway,caseService bash scripts/ops/sync-env-repo-tags.sh
# 环境变量：
#   SKIP_DB_MIGRATE=true  跳过 dbMigrate.image 更新（无 schema 变更时使用）
# 影响范围：🟡 第三方环境仓库（hci-platform-env）的 values.yaml
# =============================================================================

ENV_REPO_PATH="${ENV_REPO_PATH:-}"
TARGET_ENV="${TARGET_ENV:-dev}"
IMAGE_TAG="${IMAGE_TAG:-}"
# 默认空（fail-safe）：禁止无差别推进全量业务服务 tag。
# 必须由调用方显式传入本次实际构建的服务列表（来自 CI 的 deploy_services），
# 否则脚本只更新 dbMigrate 等独立镜像，绝不触碰业务服务 tag。
# 背景：纯 DB/文档/CI 配置改动不会重建业务镜像，若把业务服务 tag
# 推进到该 commit 的 sha 标签，而 ghcr 上该标签镜像不存在，会导致
# ArgoCD 新 Pod ImagePullBackOff、应用永久 Progressing（见 2026-08-13 卡死事件）。
SERVICES_CSV="${SERVICES_CSV:-}"
# 镜像仓库前缀（与 values.yaml 中的 global.imageRegistry 一致）
IMAGE_REGISTRY="${IMAGE_REGISTRY:-ghcr.io/tomturing/hci-troubleshoot-platform}"
# 设为 true 时跳过 dbMigrate.image 更新（本次无 schema 变更）
SKIP_DB_MIGRATE="${SKIP_DB_MIGRATE:-false}"

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

# fail-safe：未显式给出业务服务清单时，绝不更新任何业务服务 tag。
# 调用方（CI / env-repo-sync）必须传入本次实际构建的服务列表；
# 纯 DB / 文档 / CI 配置改动对应的 has_deploy_services=false，应传空或不传，
# 由 SKIP_BUSINESS_TAGS=true 显式确认“本次无业务镜像需同步”。
if [[ -z "$SERVICES_CSV" ]]; then
  if [[ "${SKIP_BUSINESS_TAGS:-false}" != "true" ]]; then
    echo "错误：SERVICES_CSV 为空，但未设置 SKIP_BUSINESS_TAGS=true。" >&2
    echo "这表明本次同步没有明确“要更新的业务服务清单”，为避免无差别" >&2
    echo "推进全量业务 tag（会导致 ghcr 缺失镜像 → ArgoCD 卡死），已中止。" >&2
    echo "若确属纯 DB/文档/配置改动（无业务镜像构建），请显式传入 SKIP_BUSINESS_TAGS=true。" >&2
    exit 1
  fi
  echo "SERVICES_CSV 为空且 SKIP_BUSINESS_TAGS=true：跳过全部业务服务 tag 更新。"
fi

VALUES_FILE="${ENV_REPO_PATH}/environments/${TARGET_ENV}/values.yaml"
if [[ ! -f "$VALUES_FILE" ]]; then
  echo "未找到目标 values 文件: $VALUES_FILE"
  exit 1
fi

update_service_tag() {
  local file="$1"
  local service_key="$2"
  local repository="$3"
  local tag="$4"
  local tmp

  tmp="$(mktemp)"
  if ! awk -v key="$service_key" -v repository="$repository" -v tag="$tag" '
    BEGIN { in_block=0; in_image=0; found=0; updated=0 }
    {
      if ($0 ~ "^" key ":[[:space:]]*$") {
        found=1
        in_block=1
        in_image=0
        print
        next
      }
      if (in_block && $0 ~ /^[^[:space:]#]/) {
        in_block=0
        in_image=0
      }
      if (in_block && $0 ~ /^  image:[[:space:]]*$/) {
        in_image=1
      } else if (in_image && $0 ~ /^  [^[:space:]]/) {
        in_image=0
      }
      if (in_image && $0 ~ /^    tag:[[:space:]]*/) {
        print "    tag: \"" tag "\""
        updated=1
        next
      }
      print
    }
    END {
      if (!found) {
        if (NR > 0) print ""
        print key ":"
        print "  image:"
        print "    repository: " repository
        print "    tag: \"" tag "\""
      } else if (!updated) {
        exit 42
      }
    }
  ' "$file" > "$tmp"; then
    rm -f "$tmp"
    echo "错误：${service_key} 已存在，但缺少可更新的 image.tag" >&2
    return 1
  fi
  mv "$tmp" "$file"
}

service_repository() {
  case "$1" in
    apiGateway) echo "api-gateway" ;;
    caseService) echo "case-service" ;;
    conversationService) echo "conversation-service" ;;
    agentService) echo "agent-service" ;;
    evalService) echo "eval-service" ;;
    diagnosisService) echo "diagnosis-service" ;;
    schedulerService) echo "scheduler-service" ;;
    kbService) echo "kb-service" ;;
    customerUI) echo "customer-ui" ;;
    adminUI) echo "admin-ui" ;;
    terminalBridge) echo "terminal-bridge" ;;
    dbMigrate) echo "db-migrate" ;;
    *)
      echo "错误：未知服务 key：$1" >&2
      return 1
      ;;
  esac
}

if [[ -n "$SERVICES_CSV" ]]; then
  IFS=',' read -r -a services <<< "$SERVICES_CSV"
  for svc in "${services[@]}"; do
    repository=""
    # 跳过来自独立仓库的服务，防止 tag 被 htp CI 错误覆盖
    if echo ",${BLOCKED_SERVICES}," | grep -q ",${svc},"; then
      echo "⚠️  跳过 ${svc}（在保护名单中，tag 来自独立仓库）"
      continue
    fi
    repository="$(service_repository "$svc")"
    echo "更新 ${svc}.image.tag -> ${IMAGE_TAG}"
    update_service_tag "$VALUES_FILE" "$svc" "$repository" "$IMAGE_TAG"
  done
else
  echo "本次没有业务服务镜像需要同步，仅处理 dbMigrate（如有）"
fi

# dbMigrate 使用嵌套结构（image.repository + image.tag），只需更新 tag 字段
# 复用 update_service_tag，以 dbMigrate 为块键匹配
update_db_migrate_tag() {
  local file="$1"
  local tag="$2"
  update_service_tag "$file" "dbMigrate" "db-migrate" "$tag"
}

if [[ "${SKIP_DB_MIGRATE}" == "true" ]]; then
  echo "跳过 dbMigrate.image.tag 更新（SKIP_DB_MIGRATE=true，本次无 schema 变更）"
else
  echo "更新 dbMigrate.image.tag -> ${IMAGE_TAG}"
  update_db_migrate_tag "$VALUES_FILE" "$IMAGE_TAG"
fi

echo "同步完成: ${VALUES_FILE}"
