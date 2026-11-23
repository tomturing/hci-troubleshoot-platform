#!/usr/bin/env bash
# 把 WeKnora 的上游镜像同步到私有 registry。
#
# 用途：为离线/私有客户环境准备不依赖外网的镜像副本。staging 当前按环境仓
# values 直拉上游镜像；离线环境则把下面六个镜像同批同步并切换 repository，
# 避免 full values 已启用但 Pod 因缺少 Neo4j/MinIO 副本而 ImagePullBackOff。
#
# 依赖 crane（https://github.com/google/go-containerregistry），无需 docker daemon。
# 用法：
#   REGISTRY=ghcr.io/tomturing/hci-troubleshoot-platform \
#   crane login ghcr.io -u <user>   # 或提前 docker login
#   ./weknora/scripts/sync-upstream-images.sh v0.9.0
set -euo pipefail

VERSION="${1:-v0.9.0}"
REGISTRY="${REGISTRY:-ghcr.io/tomturing/hci-troubleshoot-platform}"
CRANE="${CRANE:-crane}"

if ! command -v "${CRANE}" >/dev/null 2>&1; then
    echo "未找到 crane，请先安装：https://github.com/google/go-containerregistry/releases" >&2
    exit 1
fi

# 上游 tag 是 v0.9.0；0.9.0 在 Docker Hub 上不存在（会 404），不要改。
copy() {
    local src="$1" dst="$2"
    echo "[sync] ${src} -> ${REGISTRY}/${dst}"
    "${CRANE}" copy "${src}" "${REGISTRY}/${dst}"
}

copy "wechatopenai/weknora-app:${VERSION}"            "weknora-app:${VERSION}"
copy "wechatopenai/weknora-docreader:${VERSION}"      "weknora-docreader:${VERSION}"
copy "paradedb/paradedb:v0.25.11-pg17"                "weknora-postgres:v0.25.11-pg17"
copy "redis:7.0-alpine"                               "weknora-redis:7.0-alpine"
copy "neo4j:2025.10.1"                                "weknora-neo4j:2025.10.1"
copy "pgsty/minio:RELEASE.2026-08-04T00-00-00Z"       "weknora-minio:RELEASE.2026-08-04T00-00-00Z"

echo "[sync] 完成。请把以下 tag 写进 hci-platform-env 的 weknora-values.yaml："
echo "  app/docreader: ${VERSION}"
echo "  postgres:      v0.25.11-pg17"
echo "  redis:         7.0-alpine"
echo "  neo4j:         2025.10.1"
echo "  minio:         RELEASE.2026-08-04T00-00-00Z"
