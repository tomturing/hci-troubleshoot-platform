#!/usr/bin/env bash
# 把 WeKnora 的上游镜像同步到私有 registry。
#
# 为什么必须同步：集群节点的 Docker Hub 加速器对第三方 namespace 返回
# 403 Forbidden（wechatopenai/* 实测），kubelet 直连 docker.io 也不可达，
# 所以 k8s 只能消费私有 registry 里的副本。
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

echo "[sync] 完成。请把以下 tag 写进 hci-platform-env 的 weknora-values.yaml："
echo "  app/docreader: ${VERSION}"
echo "  postgres:      v0.25.11-pg17"
echo "  redis:         7.0-alpine"
