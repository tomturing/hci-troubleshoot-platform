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

# 版本清单：与 exe 同一次构建、同一组变量产出，前端据此判断客户机上正在跑的
# bridge 是否就是平台当前分发的那个版本。
#
# 为什么必须由构建产出、不能前端写死：版本号只在构建时才知道（git describe）。
# 为什么只在 windows/amd64 时写：本清单描述的是**下载分发目录里的那个可执行文件**；
# 本地或 CI 用 GOOS=linux 跑一次冒烟构建就把清单改成 linux 版本，会让所有客户
# 被误报「版本过旧」——这是本机制最容易被无意打破的地方。
if [ "$GOOS" = "windows" ] && [ "$GOARCH" = "amd64" ]; then
  MANIFEST="$DST_DIR/bridge-version.json"
  printf '{\n  "version": "%s",\n  "commit": "%s",\n  "build_time": "%s",\n  "goos": "%s",\n  "goarch": "%s"\n}\n' \
    "$GIT_VER" "$GIT_COMMIT" "$BUILD_TIME" "$GOOS" "$GOARCH" > "$MANIFEST"
  echo "[build_terminal_bridge] 版本清单 -> $MANIFEST ($GIT_VER)"
fi
