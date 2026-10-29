#!/bin/sh
#
# DSH 容器入口脚本
#
# 职责：
#   1. 以 loopback 绑定启动 `dsh --profile web`，对外访问交给同 Pod 的 nginx sidecar；
#   2. 从 dsh 的 stdout 抓取形如 `dsh web: <url>?token=<t>` 的启动行，把其中的
#      launch token 原子写入 /run/dsh/token，供 sidecar 的 /__internal/token 读取；
#   3. 把 dsh 的 stdout/stderr 持续透传到容器日志，保证 kubectl logs 可见。
#
# 环境变量（由 Helm Chart / Docker Compose 注入）：
#   DSH_PORT          dsh 监听端口，默认 3080
#   DSH_PUBLIC_URL    对外根地址，形如 https://acli.sangfor.com.cn/dsh/
#   DSH_TRUSTED_HOST  上游 Host/Origin 白名单，形如 acli.sangfor.com.cn
#   DSH_PROFILE       profile 名，默认 web
#   DSH_PROFILE_PATCH_SOURCE
#                     可选的只读 patch 文件；启动前复制到 profile 家目录
#   DSH_PLUGIN_PACKAGES
#                     以空格分隔的 npm bundle 包（可带版本），启动前通过
#                     `dsh plugin --profile web add` 安装到 profile
#
# 说明：token 只在抓取成功的那一刻写入一次。它每次进程启动都会变化，
# 因此消费方（api-gateway）每次请求都实时拉取，不缓存、不落盘到集群外。
set -eu

DSH_PORT="${DSH_PORT:-3080}"
DSH_PUBLIC_URL="${DSH_PUBLIC_URL:-http://127.0.0.1:${DSH_PORT}/}"
DSH_TRUSTED_HOST="${DSH_TRUSTED_HOST:-localhost}"
DSH_PROFILE="${DSH_PROFILE:-web}"
DSH_HOME="${DSH_HOME:-${HOME}/.dsh}"
TOKEN_DIR="${TOKEN_DIR:-/run/dsh}"
LOG_FILE="${TOKEN_DIR}/dsh.log"
TOKEN_FILE="${TOKEN_DIR}/token"
PROFILE_DIR="${DSH_HOME}/profiles/${DSH_PROFILE}"

mkdir -p "$PROFILE_DIR"

# 先安装 profile bundle，再写入用户 patch。`dsh plugin add` 会维护
# package.json 的 dependencies 与 dsh.profile.bundles，避免只写 patch 却因
# Node 无法解析插件包而在加载期失败。包名使用空格分隔，适配 scoped npm 包。
if [ -n "${DSH_PLUGIN_PACKAGES:-}" ]; then
    for PACKAGE in $DSH_PLUGIN_PACKAGES; do
        echo "[dsh-entrypoint] installing profile plugin: ${PACKAGE}"
        dsh plugin --profile "$DSH_PROFILE" add "$PACKAGE"
    done
fi

# ConfigMap/bind mount 只读，部署基线作为 launcher 的 --patch invocation layer
# 传给 DSH；不能直接写 profile cordis.patch.yml，否则 Web 设置页保存时会覆盖部署基线。
if [ -n "${DSH_PROFILE_PATCH_SOURCE:-}" ]; then
    if [ ! -f "$DSH_PROFILE_PATCH_SOURCE" ]; then
        echo "[dsh-entrypoint] ERROR: profile patch not found: ${DSH_PROFILE_PATCH_SOURCE}" >&2
        exit 1
    fi
    DSH_RUNTIME_PATCH="${DSH_RUNTIME_PATCH:-${DSH_PROFILE_PATCH_SOURCE}}"
fi

mkdir -p "$TOKEN_DIR"
: > "$LOG_FILE"

# 只绑定 127.0.0.1：非 loopback 绑定会触发上游的暴露告警，
# 而 0.0.0.0 会被上游在加载期直接拒绝。
set -- dsh \
    --profile "$DSH_PROFILE"
if [ -n "${DSH_RUNTIME_PATCH:-}" ]; then
    set -- "$@" --patch "$DSH_RUNTIME_PATCH"
fi
set -- "$@" \
    --host 127.0.0.1 \
    --port "$DSH_PORT" \
    --public-url "$DSH_PUBLIC_URL" \
    --trusted-host "$DSH_TRUSTED_HOST" \
    --no-open
"$@" > "$LOG_FILE" 2>&1 &
DSH_PID=$!

# 把日志持续转发到容器 stdout，避免重定向到文件后 kubectl logs 看不到内容
tail -f "$LOG_FILE" &
TAIL_PID=$!

# 轮询等待上游就绪行。该行在 Loader 树就绪后才打印，是官方 readiness 信号，
# 因此拿到它就代表 dsh 已经可以服务。
TOKEN=""
I=0
while [ "$I" -lt 120 ]; do
    LINE=$(grep -m1 '^dsh web: ' "$LOG_FILE" 2>/dev/null || true)
    if [ -n "$LINE" ]; then
        # 形如 `dsh web: https://host/dsh/?token=xxx`，取 token= 之后的部分
        TOKEN="${LINE##*token=}"
        break
    fi
    I=$((I + 1))
    sleep 1
done

if [ -n "$TOKEN" ]; then
    # 原子写入：先写临时文件再 mv，防止 sidecar 读到半截内容
    printf '%s' "$TOKEN" > "${TOKEN_FILE}.tmp"
    mv -f "${TOKEN_FILE}.tmp" "$TOKEN_FILE"
else
    echo "[dsh-entrypoint] WARN: 120s 内未捕获 launch token，/dsh 会话需等待上游就绪后重建" >&2
fi

# 前台等待 dsh 退出：dsh 挂了容器就退出，交给 kubelet 重启并重新抓取 token
set +e
wait "$DSH_PID"
CODE=$?
kill "$TAIL_PID" 2>/dev/null || true
exit "$CODE"
