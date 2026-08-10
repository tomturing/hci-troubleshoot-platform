#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

# C3 dev 验收入口：一次命令完成 KBD → Bundle → hci-sim，第二步只需在 Custom UI
# 粘贴 connection.json 中的连接字段。脚本不打印 HMAC key/Lease，避免凭据进入终端历史、截图或 CI 日志。

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
KBD_ID="${1:-}"
if [[ ! "$KBD_ID" =~ ^[0-9]+$ ]]; then
  echo "用法: $0 <KBD_ID>" >&2
  exit 2
fi

RUN_ID="${KBD_ID}-$(date -u +%Y%m%d%H%M%S)"
RUN_DIR="${HCI_SIM_RUN_DIR:-$ROOT_DIR/.hci-sim-run/$RUN_ID}"
IMAGE="${HCI_SIM_IMAGE:-hci-sim:c3}"
CONTAINER="hci-sim-c3-${RUN_ID}"
CAP_PORT="${HCI_SIM_CAPABILITIES_PORT:-18004}"
CAP_URL="${HCI_SIM_CAPABILITIES_URL:-http://127.0.0.1:${CAP_PORT}/api/kb/hci-sim/capabilities}"
SSH_PORT="${HCI_SIM_SSH_PORT:-2222}"
HTTP_PORT="${HCI_SIM_HTTP_PORT:-18080}"
AUTO_PORTS="${HCI_SIM_AUTO_PORTS:-true}"
DEFAULT_CONNECTION_HOST="$(hostname -I 2>/dev/null | awk '{print $1}')"
DEFAULT_CONNECTION_HOST="${DEFAULT_CONNECTION_HOST:-127.0.0.1}"
CONNECTION_HOST="${HCI_SIM_CONNECTION_HOST:-$DEFAULT_CONNECTION_HOST}"
LEASE_KEY="${HCI_SIM_LEASE_HMAC_KEY:-$(openssl rand -hex 32)}"
LEASE_TTL="${HCI_SIM_LEASE_TTL:-15m}"
HOST_KEY_STATE_DIR="${HCI_SIM_HOST_KEY_STATE_DIR:-$ROOT_DIR/.hci-sim-state}"
HOST_KEY_STATE_FILE="${HCI_SIM_HOST_KEY_FILE:-$HOST_KEY_STATE_DIR/ssh_host_key}"

command -v docker >/dev/null 2>&1 || { echo "缺少 docker" >&2; exit 1; }
command -v curl >/dev/null 2>&1 || { echo "缺少 curl" >&2; exit 1; }
command -v openssl >/dev/null 2>&1 || { echo "缺少 openssl" >&2; exit 1; }
command -v ssh-keygen >/dev/null 2>&1 || { echo "缺少 ssh-keygen" >&2; exit 1; }
if [[ -z "${HCI_SIM_CAPABILITIES_URL:-}" ]] && ! command -v kubectl >/dev/null 2>&1; then
  echo "未提供 HCI_SIM_CAPABILITIES_URL，且当前 PATH 找不到 kubectl；请安装 kubectl 或显式设置 capabilities URL。" >&2
  exit 1
fi

mkdir -p "$RUN_DIR"
chmod 700 "$RUN_DIR"
if [[ -e "$RUN_DIR/fixture-manifest.json" || -e "$RUN_DIR/connection.json" || -e "$RUN_DIR/ssh_host_key" ]]; then
  echo "运行目录已包含 Bundle 或密钥，请指定新的 HCI_SIM_RUN_DIR" >&2
  exit 1
fi

# 同一个 KBD 的验收入口只保留一个活动实例。这样不会出现“新 Lease 连接到旧容器”的错配。
while IFS= read -r old_container; do
  [[ "$old_container" == hci-sim-c3-${KBD_ID}-* ]] || continue
  echo "停止同一 KBD 的旧仿真容器：$old_container" >&2
  docker stop "$old_container" >/dev/null || true
done < <(docker ps --format '{{.Names}}')

port_is_open() {
  (exec 3<>"/dev/tcp/127.0.0.1/$1") >/dev/null 2>&1
}

pick_free_port() {
  local requested="$1" first="$2" last="$3" candidate
  if ! port_is_open "$requested"; then
    printf '%s' "$requested"
    return 0
  fi
  if [[ "$AUTO_PORTS" != "true" ]]; then
    return 1
  fi
  for candidate in $(seq "$first" "$last"); do
    if ! port_is_open "$candidate"; then
      printf '%s' "$candidate"
      return 0
    fi
  done
  return 1
}

# 默认仍优先使用 2222/18080；若被其他验收实例占用，自动选择隔离端口。
# 显式设置 HCI_SIM_*_PORT 时可通过 HCI_SIM_AUTO_PORTS=false 强制失败，便于 CI 捕获配置错误。
ORIGINAL_SSH_PORT="$SSH_PORT"
ORIGINAL_HTTP_PORT="$HTTP_PORT"
if ! SSH_PORT="$(pick_free_port "$SSH_PORT" 22000 22999)"; then
  echo "SSH 端口 ${ORIGINAL_SSH_PORT} 仍被占用，且没有可用候选端口" >&2
  exit 1
fi
if ! HTTP_PORT="$(pick_free_port "$HTTP_PORT" 18081 18999)"; then
  echo "HTTP 端口 ${ORIGINAL_HTTP_PORT} 仍被占用，且没有可用候选端口" >&2
  exit 1
fi
if [[ "$SSH_PORT" != "$ORIGINAL_SSH_PORT" || "$HTTP_PORT" != "$ORIGINAL_HTTP_PORT" ]]; then
  echo "默认端口被占用，已为本次运行选择隔离端口：SSH=${SSH_PORT} HTTP=${HTTP_PORT}" >&2
fi
if [[ -z "${HCI_SIM_CAPABILITIES_URL:-}" ]]; then
  ORIGINAL_CAP_PORT="$CAP_PORT"
  if ! CAP_PORT="$(pick_free_port "$CAP_PORT" 18400 18499)"; then
    echo "Capabilities 端口 ${ORIGINAL_CAP_PORT} 仍被占用，且没有可用候选端口" >&2
    exit 1
  fi
  CAP_URL="http://127.0.0.1:${CAP_PORT}/api/kb/hci-sim/capabilities"
  if [[ "$CAP_PORT" != "$ORIGINAL_CAP_PORT" ]]; then
    echo "Capabilities 端口被占用，已选择隔离端口：${CAP_PORT}" >&2
  fi
fi

# hci-sim 每次运行都重新生成 host key 会触发 Terminal Bridge 的 known_hosts 冲突。
# dev 验收使用主机级稳定 key；生产环境不使用此脚本，也不复用该 key。
mkdir -p "$HOST_KEY_STATE_DIR"
chmod 700 "$HOST_KEY_STATE_DIR"
if [[ ! -s "$HOST_KEY_STATE_FILE" ]]; then
  ssh-keygen -q -t rsa -b 2048 -N '' -f "$HOST_KEY_STATE_FILE"
  chmod 600 "$HOST_KEY_STATE_FILE"
fi
cp "$HOST_KEY_STATE_FILE" "$RUN_DIR/ssh_host_key"
chmod 600 "$RUN_DIR/ssh_host_key"

# 在当前 dev 集群中自动接通 C1 内部 API；若调用者已提供 URL，则不触碰 Kubernetes。
PF_PID=""
if [[ -z "${HCI_SIM_CAPABILITIES_URL:-}" ]]; then
  TOKEN="${INTERNAL_API_TOKEN:-}"
  if [[ -z "$TOKEN" ]]; then
    TOKEN="$(kubectl -n hci-dev exec deploy/kb-service -- printenv INTERNAL_API_TOKEN)"
  fi
  kubectl -n hci-dev port-forward svc/kb-service "${CAP_PORT}:8004" >/tmp/hci-sim-c3-port-forward.log 2>&1 &
  PF_PID=$!
  trap 'kill "$PF_PID" 2>/dev/null || true' EXIT
  capabilities_ready=0
  for _ in {1..30}; do
    if curl -fsS "$CAP_URL/$KBD_ID" -H "Authorization: Bearer ${TOKEN}" >/dev/null 2>&1; then
      capabilities_ready=1
      break
    fi
    if ! kill -0 "$PF_PID" 2>/dev/null; then
      echo "kubectl port-forward 已提前退出：$(sed -n '1p' /tmp/hci-sim-c3-port-forward.log 2>/dev/null || true)" >&2
      break
    fi
    sleep 0.2
  done
  if [[ "$capabilities_ready" != 1 ]]; then
    echo "C1 capabilities 在 6 秒内不可用：$CAP_URL/$KBD_ID" >&2
    exit 1
  fi
fi

if [[ ! -f "$ROOT_DIR/hci_sim/cmd/hci-sim/bootstrap.go" ]]; then
  echo "缺少 C3 bootstrap 编译器" >&2
  exit 1
fi

echo "[1/2] 构建并启动 KBD ${KBD_ID} 的 synthetic positive-minimal 环境..."
docker build --quiet -t "$IMAGE" -f "$ROOT_DIR/hci_sim/Dockerfile" "$ROOT_DIR" >/dev/null

TOKEN="${INTERNAL_API_TOKEN:-}"
if [[ -z "$TOKEN" ]] && command -v kubectl >/dev/null 2>&1; then
  TOKEN="$(kubectl -n hci-dev exec deploy/kb-service -- printenv INTERNAL_API_TOKEN)"
fi

docker run --rm --user "$(id -u):$(id -g)" --network host \
  -e "HCI_SIM_CAPABILITIES_URL=${CAP_URL}" \
  -e "INTERNAL_API_TOKEN=${TOKEN}" \
  -e "HCI_SIM_LEASE_HMAC_KEY=${LEASE_KEY}" \
  -e HCI_SIM_LEASE_ISSUER=hci-platform -e HCI_SIM_LEASE_AUDIENCE=hci-sim \
  -v "$RUN_DIR:/run/hci-sim" "$IMAGE" bootstrap --kbd-id "$KBD_ID" \
  --capabilities-url "$CAP_URL" --api-token "$TOKEN" --lease-key "$LEASE_KEY" --output-dir /run/hci-sim \
  --connection-host "$CONNECTION_HOST" --connection-port "$SSH_PORT" --ttl "$LEASE_TTL" >"$RUN_DIR/bootstrap.json"

chmod 600 "$RUN_DIR/connection.json" "$RUN_DIR/fixture-manifest.json" "$RUN_DIR/ssh_host_key"
docker run -d --name "$CONTAINER" --network host \
  --label com.hci.acceptance=true --label "com.hci.kbd-id=${KBD_ID}" \
  -e HCI_SIM_FIXTURE_MANIFEST=/run/hci-sim/fixture-manifest.json \
  -e HCI_SIM_HOST_KEY_FILE=/run/hci-sim/ssh_host_key \
  -e "HCI_SIM_SSH_LISTEN=:${SSH_PORT}" -e "HCI_SIM_HTTP_LISTEN=:${HTTP_PORT}" \
  -e "HCI_SIM_LEASE_HMAC_KEY=${LEASE_KEY}" -e HCI_SIM_LEASE_ISSUER=hci-platform -e HCI_SIM_LEASE_AUDIENCE=hci-sim \
  -v "$RUN_DIR:/run/hci-sim:ro" "$IMAGE" >/dev/null

ready=0
for _ in {1..50}; do
  if curl -fsS "http://127.0.0.1:${HTTP_PORT}/readyz" >/dev/null 2>&1 && port_is_open "$SSH_PORT"; then
    ready=1
    break
  fi
  sleep 0.2
done
if [[ "$ready" != 1 ]]; then
  echo "hci-sim 启动失败：readyz 在 10 秒内未通过" >&2
  docker inspect "$CONTAINER" --format 'status={{.State.Status}} exit={{.State.ExitCode}} error={{.State.Error}}' >&2 || true
  docker logs "$CONTAINER" >&2 || true
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  exit 1
fi

echo "环境已启动：$CONTAINER"
echo "[2/2] Custom UI → SSH 终端：打开 connection.json，选择‘仿真租约’，按字段连接后执行 recommended_command。"
echo "连接文件（Lease 为一次性敏感能力，不在终端打印）：$RUN_DIR/connection.json"
python3 - "$RUN_DIR/connection.json" <<'PY'
import json, sys
with open(sys.argv[1], encoding='utf-8') as handle:
    data = json.load(handle)
connection = data["connection"]
print(json.dumps({
    "support_id": data.get("support_id"),
    "test_run_id": connection.get("test_run_id"),
    "host": connection.get("host"),
    "port": connection.get("port"),
    "username": connection.get("username"),
    "auth_type": connection.get("auth_type"),
    "execution_mode": connection.get("execution_mode"),
    "expires_at": data.get("expires_at"),
    "recommended_command": data.get("recommended_command"),
    "password": "<REDACTED>",
}, ensure_ascii=False, indent=2))
PY
echo "停止环境：docker stop $CONTAINER"
