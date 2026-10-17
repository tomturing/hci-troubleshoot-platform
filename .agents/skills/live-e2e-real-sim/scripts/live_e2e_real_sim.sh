#!/usr/bin/env bash
# live_e2e_real_sim.sh —— HCI 平台「真仿真端到端」live E2E 验证 runbook
#
# 编排可通用的部分：集群内门禁 → 离线构建含修复的 terminal_bridge 二进制 → 热替换隔离 bridge Pod
# → 现签 lease → 驱动 live E2E smoke（真采集闭环）→ 判票 → 清理。
# 场景专有前置（hci-sim fixture server + lease secret + bundle configmap）需先按该场景的
# fixtures/<scenario>/sandbox-pod.yaml 单独起好；本脚本默认假设其已 Running 且 Service 就绪。
#
# 设计依据：.agents/skills/live-e2e-real-sim/skill.md（第一性原理硬约束 + 失败模式表）。
set -euo pipefail

# ── 默认参数（均可 CLI 覆盖）────────────────────────────────────────────
NS="${NS:-hci-sim-live-e2e}"
SRC_DIR="${SRC_DIR:-}"        # 承载 bridge 修复的 terminal_bridge 目录（通常是 worktree 内路径）
BIN_DIR="${BIN_DIR:-}"        # 构建产物落盘目录（hostPath，供 bridge Pod 挂载；一般在 fixtures/<scenario>/bin）
GOMOD_DIR="${GOMOD_DIR:-$HOME/go/pkg/mod}"   # 宿主机 Go 模块缓存（离线构建源）
REPO_ROOT="${REPO_ROOT:-$(pwd)}"             # 运行 smoke 的仓库根（含 evaluation/live_e2e）
SCENARIO="${SCENARIO:-disk_badtrack}"
FIXTURES_DIR="${FIXTURES_DIR:-$REPO_ROOT/evaluation/live_e2e/fixtures/$SCENARIO}"
CONNECTION="${CONNECTION:-$FIXTURES_DIR/connection.json}"
BRIDGE_IMAGE="${BRIDGE_IMAGE:-ghcr.io/tomturing/hci-troubleshoot-platform/terminal-bridge:latest}"
HCI_SIM_POD="${HCI_SIM_POD:-hci-sim-$SCENARIO}"
HCI_SIM_SVC="${HCI_SIM_SVC:-hci-sim-$SCENARIO}"
ATTEMPTS="${ATTEMPTS:-3}"
MAX_TURNS="${MAX_TURNS:-12}"
DUMP_DIR="${DUMP_DIR:-$REPO_ROOT/.local/live_e2e/simrun}"
INTERNAL_TOKEN="${INTERNAL_TOKEN:-}"
GOLANG_IMAGE="${GOLANG_IMAGE:-golang:1.26.6-alpine}"
DO_CLEANUP=1
DRY_RUN=0

log() { printf '\033[1;36m[runbook]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[runbook][FATAL]\033[0m %s\n' "$*" >&2; exit 1; }
run() { if [[ $DRY_RUN -eq 1 ]]; then log "DRY: $*"; else eval "$@"; fi; }

usage() {
  cat <<'EOF'
用法: live_e2e_real_sim.sh [选项]
  --src <dir>          terminal_bridge 源目录（含修复，hostPath 挂载进构建 Pod）[必填]
  --bin <dir>          构建产物落盘目录（hostPath，bridge Pod 挂载）[必填]
  --repo <dir>         运行 smoke 的仓库根 (默认: 当前目录)
  --scenario <name>    场景名 (默认: disk_badtrack)
  --ns <name>          隔离命名空间 (默认: hci-sim-live-e2e)
  --attempts <n>       多数表决次数 (默认: 3)
  --dump-dir <dir>     smoke 事件落盘目录
  --internal-token <t> agent-service 内部令牌 (或环境变量 INTERNAL_TOKEN)
  --no-cleanup         跑完不删隔离 ns（排障用）
  --dry-run            只打印将执行的命令，不实际执行
  -h | --help          显示本帮助
EOF
}

# ── 解析 CLI ────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --src) SRC_DIR="$2"; shift 2 ;;
    --bin) BIN_DIR="$2"; shift 2 ;;
    --repo) REPO_ROOT="$2"; shift 2 ;;
    --scenario) SCENARIO="$2"; shift 2 ;;
    --ns) NS="$2"; shift 2 ;;
    --attempts) ATTEMPTS="$2"; shift 2 ;;
    --dump-dir) DUMP_DIR="$2"; shift 2 ;;
    --internal-token) INTERNAL_TOKEN="$2"; shift 2 ;;
    --no-cleanup) DO_CLEANUP=0; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "未知参数: $1（--help 看用法）" ;;
  esac
done

[[ -n "$SRC_DIR" && -d "$SRC_DIR" ]] || { usage; die "--src 指向的 terminal_bridge 目录不存在"; }
[[ -n "$BIN_DIR" ]] || die "--bin 必填（构建产物 hostPath 落盘目录）"
command -v kubectl >/dev/null || die "需要 kubectl"
command -v uv >/dev/null || die "需要 uv（Python 环境管理）"

mkdir -p "$BIN_DIR" "$DUMP_DIR"

# ── 自动发现平台服务 ClusterIP（staging 每次部署可能变，勿硬编码）─────────
svc_ip() { # $1=service 名子串
  kubectl get svc -A --no-headers 2>/dev/null | awk -v n="$1" '$2 ~ n {print $4; exit}'
}
CASE_IP="$(svc_ip case-service || true)"
CONV_IP="$(svc_ip conversation-service || true)"
AGENT_IP="$(svc_ip agent-service || true)"
log "发现服务: case=$CASE_IP conv=$CONV_IP agent=$AGENT_IP"
[[ -n "$CASE_IP" && -n "$CONV_IP" && -n "$AGENT_IP" ]] || \
  die "未能自动发现全部平台服务 ClusterIP，请检查部署或用环境变量指定"

# ── teardown trap ───────────────────────────────────────────────────────
cleanup() {
  if [[ $DO_CLEANUP -eq 1 ]]; then
    log "清理隔离 ns: $NS"
    run "kubectl delete ns '$NS' --wait=false || true"
  else
    log "--no-cleanup: 保留 ns $NS 供排障"
  fi
}
trap cleanup EXIT

# ── 1) 建隔离 ns ────────────────────────────────────────────────────────
run "kubectl create ns '$NS' --dry-run=client -o yaml | kubectl apply -f -"

# ── 2) 集群内门禁：gofmt/vet/test（内联生成 Pod，hostPath 指向 --src）────
render_gate_pod() {
  cat <<EOF
apiVersion: v1
kind: Pod
metadata: { name: bridge-gate-verify, namespace: $NS }
spec:
  restartPolicy: Never
  containers:
    - name: verify
      image: $GOLANG_IMAGE
      command: [sh,-c,set -e; cd /src; export GOTOOLCHAIN=local GOFLAGS=-mod=mod GOPROXY=off GOSUMDB=off GOMODCACHE=/go/pkg/mod; gofmt -l .; go vet ./...; go test ./...]
      volumeMounts:
        - { name: src, mountPath: /src }
        - { name: gomod, mountPath: /go/pkg/mod }
  volumes:
    - { name: src, hostPath: { path: $SRC_DIR, type: Directory } }
    - { name: gomod, hostPath: { path: $GOMOD_DIR, type: Directory } }
EOF
}
log "步骤2: 集群内门禁 gofmt/vet/test"
if [[ $DRY_RUN -eq 0 ]]; then
  render_gate_pod | kubectl apply -f -
  kubectl -n "$NS" wait --for=jsonpath='{.status.phase}'=Succeeded pod/bridge-gate-verify --timeout=8m || \
    { kubectl -n "$NS" logs bridge-gate-verify || true; die "门禁未通过（看上面 Go 测试输出）"; }
else
  render_gate_pod
fi

# ── 3) 集群内离线构建含修复的静态 bridge 二进制到 --bin ──────────────────
render_build_pod() {
  cat <<EOF
apiVersion: v1
kind: Pod
metadata: { name: bridge-fix-build, namespace: $NS }
spec:
  restartPolicy: Never
  containers:
    - name: build
      image: $GOLANG_IMAGE
      command: [sh,-c,set -e; cd /src; export GOTOOLCHAIN=local GOFLAGS=-mod=mod GOPROXY=off GOSUMDB=off GOMODCACHE=/go/pkg/mod; CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath -ldflags="-s -w" -o /out/terminal_bridge .; chmod 755 /out/terminal_bridge; ls -la /out]
      volumeMounts:
        - { name: src, mountPath: /src, readOnly: true }
        - { name: out, mountPath: /out }
        - { name: gomod, mountPath: /go/pkg/mod }
  volumes:
    - { name: src, hostPath: { path: $SRC_DIR, type: Directory } }
    - { name: out, hostPath: { path: $BIN_DIR, type: DirectoryOrCreate } }
    - { name: gomod, hostPath: { path: $GOMOD_DIR, type: Directory } }
EOF
}
log "步骤3: 集群内离线构建静态 bridge 二进制 → $BIN_DIR/terminal_bridge"
if [[ $DRY_RUN -eq 0 ]]; then
  render_build_pod | kubectl apply -f -
  kubectl -n "$NS" wait --for=jsonpath='{.status.phase}'=Succeeded pod/bridge-fix-build --timeout=10m || \
    { kubectl -n "$NS" logs bridge-fix-build || true; die "构建失败"; }
else
  render_build_pod
fi

# ── 4) 热替换隔离 bridge Pod（用 create 规避 apply 的 terminating 竞争）────
render_bridge_pod() {
  cat <<EOF
apiVersion: v1
kind: Pod
metadata: { name: live-e2e-bridge, namespace: $NS, labels: { app: live-e2e-bridge } }
spec:
  containers:
    - name: bridge
      image: $BRIDGE_IMAGE
      command: [/opt/bridge/terminal_bridge]
      args: [--mode=cluster, --allowed-origins=*]
      ports: [ { containerPort: 9999 } ]
      env:
        - { name: HCI_BRIDGE_LOG_LEVEL, value: DEBUG }
        - { name: HCI_BRIDGE_HOST_KEY_POLICY, value: insecure }
      volumeMounts:
        - { name: bridge-bin, mountPath: /opt/bridge, readOnly: true }
  volumes:
    - { name: bridge-bin, hostPath: { path: $BIN_DIR, type: Directory } }
EOF
}
log "步骤4: 热替换 live-e2e-bridge（delete→create 内联清单）"
if [[ $DRY_RUN -eq 0 ]]; then
  kubectl -n "$NS" delete pod live-e2e-bridge --ignore-not-found --wait=true
  render_bridge_pod | kubectl create -f -
  kubectl -n "$NS" wait --for=condition=Ready pod/live-e2e-bridge --timeout=3m
  BRIDGE_IP="$(kubectl -n "$NS" get pod live-e2e-bridge -o jsonpath='{.status.podIP}')"
  log "bridge Running, podIP=$BRIDGE_IP"
else
  render_bridge_pod; BRIDGE_IP="<bridge-pod-ip>"
fi
BRIDGE_IP="${BRIDGE_IP:-$(kubectl -n "$NS" get pod live-e2e-bridge -o jsonpath='{.status.podIP}' 2>/dev/null || echo '<bridge-pod-ip>')}"

# ── 5) 现签 lease（TTL 4h；隔时再跑务必重签）───────────────────────────────
log "步骤5: 现签 lease → $CONNECTION"
run "python3 '$FIXTURES_DIR/make_connection.py'"

# ── 6) 驱动 live E2E smoke（真采集闭环）────────────────────────────────────
log "步骤6: 跑 smoke --mode real --sim --attempts $ATTEMPTS"
SMOKE_CMD=(
  env PYTHONUNBUFFERED=1 PYTHONPATH="$REPO_ROOT/backend" INTERNAL_API_TOKEN="$INTERNAL_TOKEN"
  uv run --frozen --python 3.12 -m evaluation.live_e2e.smoke
  --mode real --sim --attempts "$ATTEMPTS" --max-turns "$MAX_TURNS"
  --case-base "http://$CASE_IP:8001"
  --conv-base "http://$CONV_IP:8002"
  --agent-base "http://$AGENT_IP:8005"
  --bridge-url "ws://$BRIDGE_IP:9999" --bridge-origin "http://$BRIDGE_IP:9999"
  --connection "$CONNECTION"
  --dump-dir "$DUMP_DIR"
)
if [[ $DRY_RUN -eq 1 ]]; then
  log "DRY: ${SMOKE_CMD[*]}"
else
  ( cd "$REPO_ROOT" && "${SMOKE_CMD[@]}" 2>&1 | tee "$DUMP_DIR/runbook.log" )
  # ── 7) 判票：从日志抓 GREEN/RED ──────────────────────────────────────
  if grep -q "GREEN ✅" "$DUMP_DIR/runbook.log"; then
    log "端到端判票: GREEN ✅ —— 达到合并门槛"
  else
    DO_CLEANUP=0   # 判绿失败保留现场
    die "端到端判票非 GREEN；已保留 ns 供排障（对照 skill.md 失败模式表定位）"
  fi
fi
