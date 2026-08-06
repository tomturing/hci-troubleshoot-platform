#!/usr/bin/env bash
set -Eeuo pipefail

# C3 dev 验收入口：一次命令完成 KBD → Bundle → hci-sim，第二步只需在 Custom UI
# 粘贴 connection.json 中的连接字段。脚本不打印 HMAC key，Lease 只落在 0600 文件。

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
LEASE_KEY="${HCI_SIM_LEASE_HMAC_KEY:-$(openssl rand -hex 32)}"

mkdir -p "$RUN_DIR"
chmod 700 "$RUN_DIR"
if [[ -e "$RUN_DIR/fixture-manifest.json" || -e "$RUN_DIR/connection.json" || -e "$RUN_DIR/ssh_host_key" ]]; then
  echo "运行目录已包含 Bundle 或密钥，请指定新的 HCI_SIM_RUN_DIR" >&2
  exit 1
fi

# 在当前 dev 集群中自动接通 C1 内部 API；若调用者已提供 URL，则不触碰 Kubernetes。
PF_PID=""
if [[ -z "${HCI_SIM_CAPABILITIES_URL:-}" ]] && command -v kubectl >/dev/null 2>&1; then
  kubectl -n hci-dev port-forward svc/kb-service "${CAP_PORT}:8004" >/tmp/hci-sim-c3-port-forward.log 2>&1 &
  PF_PID=$!
  trap 'kill "$PF_PID" 2>/dev/null || true' EXIT
  for _ in {1..30}; do
    curl -fsS "$CAP_URL/27736" -H "Authorization: Bearer ${INTERNAL_API_TOKEN:-$(kubectl -n hci-dev exec deploy/kb-service -- printenv INTERNAL_API_TOKEN)}" >/dev/null 2>&1 && break
    sleep 0.2
  done
fi

if [[ ! -f "$ROOT_DIR/hci_sim/cmd/hci-sim/bootstrap.go" ]]; then
  echo "缺少 C3 bootstrap 编译器" >&2
  exit 1
fi

echo "[1/2] 构建并启动 KBD ${KBD_ID} 的 synthetic positive-minimal 环境..."
docker build --quiet -t "$IMAGE" -f "$ROOT_DIR/hci_sim/Dockerfile" "$ROOT_DIR" >/dev/null
ssh-keygen -q -t rsa -b 2048 -N '' -f "$RUN_DIR/ssh_host_key"
chmod 644 "$RUN_DIR/fixture-manifest.json" "$RUN_DIR/ssh_host_key" 2>/dev/null || true

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
  --capabilities-url "$CAP_URL" --api-token "$TOKEN" --lease-key "$LEASE_KEY" --output-dir /run/hci-sim --connection-port "$SSH_PORT" >/tmp/hci-sim-c3-bootstrap.json

chmod 600 "$RUN_DIR/connection.json"
docker run -d --name "$CONTAINER" --network host \
  -e HCI_SIM_FIXTURE_MANIFEST=/run/hci-sim/fixture-manifest.json \
  -e HCI_SIM_HOST_KEY_FILE=/run/hci-sim/ssh_host_key \
  -e "HCI_SIM_SSH_LISTEN=:${SSH_PORT}" -e "HCI_SIM_HTTP_LISTEN=:${HTTP_PORT}" \
  -e "HCI_SIM_LEASE_HMAC_KEY=${LEASE_KEY}" -e HCI_SIM_LEASE_ISSUER=hci-platform -e HCI_SIM_LEASE_AUDIENCE=hci-sim \
  -v "$RUN_DIR:/run/hci-sim:ro" "$IMAGE" >/dev/null

for _ in {1..30}; do
  curl -fsS "http://127.0.0.1:${HTTP_PORT}/readyz" >/dev/null 2>&1 && break
  sleep 0.2
done

echo "环境已启动：$CONTAINER"
echo "[2/2] Custom UI → SSH 终端：打开 connection.json，选择‘仿真租约’，按字段连接后执行 recommended_command。"
echo "连接信息（Lease 为一次性敏感能力，仅用于本次会话）："
sed -n '1,220p' "$RUN_DIR/connection.json"
echo "停止环境：docker stop $CONTAINER"
