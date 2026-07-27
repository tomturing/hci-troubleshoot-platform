#!/usr/bin/env bash
#
# build_terminal_bridge.sh
# 构建 terminal_bridge 并将可执行文件放到 customer-ui 的下载分发目录。
#
# 用法:
#   bash scripts/build_terminal_bridge.sh                 # 默认 windows/amd64
#   GOOS=linux GOARCH=amd64 bash scripts/build_terminal_bridge.sh
#
# 说明:
#   - 消费者是 Windows 客户端，默认目标平台为 windows/amd64。
#   - 在 Windows(Git Bash) 或 Linux/WSL 上均可运行；交叉编译产出的
#     windows exe 可直接在 Win11 运行，无运行时依赖。
#   - 若环境无 go，则跳过构建（不报错），避免阻塞提交/发布流程。
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC_DIR="$ROOT/terminal_bridge"
DST_DIR="$ROOT/frontend/customer/public/downloads"
DST="$DST_DIR/terminal_bridge.exe"

# 目标平台：消费者是 Windows，默认 windows/amd64
GOOS="${GOOS:-windows}"
GOARCH="${GOARCH:-amd64}"

if ! command -v go >/dev/null 2>&1; then
  echo "[build_terminal_bridge] SKIP: 未找到 go，跳过构建"
  exit 0
fi

cd "$SRC_DIR"
[ -f go.mod ] && go mod download >/dev/null 2>&1 || true

GIT_VER="$(git -c safe.directory="$ROOT" -C "$ROOT" describe --tags --always --dirty 2>/dev/null || echo v2.16.0-dev)"
GIT_COMMIT="$(git -c safe.directory="$ROOT" -C "$ROOT" rev-parse --short=12 HEAD 2>/dev/null || echo unknown)"
BUILD_TIME="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

echo "[build_terminal_bridge] 构建 $GOOS/$GOARCH (commit: $GIT_COMMIT) ..."
CGO_ENABLED=0 GOOS="$GOOS" GOARCH="$GOARCH" \
  go build -buildvcs=false -trimpath \
  -ldflags="-s -w -buildid= -X main.Version=$GIT_VER -X main.CommitID=$GIT_COMMIT -X main.BuildTime=$BUILD_TIME" \
  -o "$DST" .

SIZE=$(stat -c%s "$DST" 2>/dev/null || wc -c < "$DST")
echo "[build_terminal_bridge] OK -> $DST ($SIZE bytes)"
