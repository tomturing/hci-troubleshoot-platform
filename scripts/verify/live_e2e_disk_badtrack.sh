#!/usr/bin/env bash
# 硬盘坏道 Live E2E Smoke 编排（发布前 gate，非每 PR CI）。
#
# 第一性原理与诚实边界：
# - 本脚本驱动 gateway 下游的 case→conversation→agent→(真实/桩)LLM→SOP 变量交互全链路，
#   断言只打「结构不变量 + 守卫事件」（见 evaluation/live_e2e/asserts.py），绝不比逐字文本。
# - 真实 LLM 非确定性 → ATTEMPTS 多数表决（≥2/3 判绿）把偶发漂移与真回归分开。
# - MODE=stub 用确定性对抗 LLM 桩（scripts/hci-sim/disk_badtrack_llm_stub.py）复刻历史坏输出，
#   证明引擎侧 filter_ungrounded_user_questions 真的拦住（对应验收标准『破坏缝隙能验红』）。
# - 依赖 docker + 全栈 compose，只能在具备环境的宿主机执行；本仓库 CI 仅跑其纯逻辑单测。
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO"

VARIANT="${VARIANT:-truncated}"            # positive | truncated
MODE="${MODE:-stub}"                        # stub | real
ATTEMPTS="${ATTEMPTS:-3}"
LEAVE_RUNNING="${LEAVE_RUNNING:-0}"         # 1=跑完不拆，便于手工排查
COMPOSE="${COMPOSE:-docker compose}"
BASE_FILE="deploy/docker/docker-compose.yml"
OVERRIDE_FILE="evaluation/live_e2e/docker-compose.live-e2e.yml"
NETWORK="hci-troubleshoot-platform_default"
STUB_CONTAINER="hci-disk-stub"
STUB_PORT="8010"
DUMP_DIR=".local/live_e2e/$(date +%Y%m%d-%H%M%S)"

# 读取 INTERNAL_API_TOKEN（身份签名 + 落库/metrics 查询共用）；优先 .env，回落 .env.example。
read_token() {
  local f
  for f in .env .env.example; do
    [ -f "$f" ] && grep -qE '^INTERNAL_API_TOKEN=' "$f" && {
      cut -d= -f2- < <(grep -E '^INTERNAL_API_TOKEN=' "$f" | head -1); return; }
  done
  echo "hci-dev-internal-token"
}
INTERNAL_API_TOKEN="${INTERNAL_API_TOKEN:-$(read_token)}"

mkdir -p "$DUMP_DIR"
echo "[live-e2e] VARIANT=$VARIANT MODE=$MODE ATTEMPTS=$ATTEMPTS 产物目录=$DUMP_DIR"

collect_logs() {
  mkdir -p "$DUMP_DIR/logs"
  local c
  for c in hci-case-service hci-conversation-service hci-agent-service "$STUB_CONTAINER"; do
    docker logs "$c" >"$DUMP_DIR/logs/${c}.log" 2>&1 || true
  done
}

cleanup() {
  echo "[live-e2e] teardown ..."
  docker rm -f "$STUB_CONTAINER" >/dev/null 2>&1 || true
  if [ "$LEAVE_RUNNING" != "1" ]; then
    $COMPOSE -f "$BASE_FILE" down >/dev/null 2>&1 || true
  fi
  echo "[live-e2e] 完成。产物: $DUMP_DIR"
}
trap cleanup EXIT

# 1) 全栈起（含 postgres/redis/db-migrate/健康）。
echo "[live-e2e] step1 make dev-up"
make dev-up

# 1b) 叠加端口暴露覆盖，把 case/conversation/agent 发布到宿主机供 .venv 驱动直连。
echo "[live-e2e] step1b 应用端口暴露覆盖 (${OVERRIDE_FILE})"
$COMPOSE --env-file .env -f "$BASE_FILE" -f "$OVERRIDE_FILE" up -d --no-deps \
  case-service conversation-service agent-service

# 2) stub 模式：在集群网内拉起确定性对抗 LLM 桩，并用它覆盖 agent-service 的 LLM_BASE_URL 后重建。
if [ "$MODE" = "stub" ]; then
  echo "[live-e2e] step2 启动对抗 LLM 桩（集群网内 ${STUB_CONTAINER}:${STUB_PORT}）"
  docker run -d --name "$STUB_CONTAINER" --network "$NETWORK" \
    -v "$REPO":/work:ro \
    -e HCI_LIVE_E2E_SCENARIO=/work/evaluation/live_e2e/scenarios/disk_badtrack.json \
    python:3.12-slim \
    python /work/scripts/hci-sim/disk_badtrack_llm_stub.py --host 0.0.0.0 --port "$STUB_PORT"
  echo "[live-e2e] step2 以桩覆盖 agent-service LLM_BASE_URL 并重建（temperature 已 pin 0）"
  LLM_BASE_URL="http://${STUB_CONTAINER}:${STUB_PORT}" LLM_API_KEY="stub-unused-not-a-secret" $COMPOSE --env-file .env -f "$BASE_FILE" -f "$OVERRIDE_FILE" up -d --no-deps --force-recreate agent-service  # pragma: allowlist secret
fi

# 3) 等服务健康（agent /metrics 可达即视为链路就绪）。
echo "[live-e2e] step3 等待服务健康"
deadline=$((SECONDS + 180))
until curl -sf "http://localhost:8005/metrics" >/dev/null 2>&1; do
  [ "$SECONDS" -lt "$deadline" ] || { echo "[live-e2e] 服务健康等待超时"; collect_logs; exit 1; }
  sleep 3
done

# 4) 宿主机 .venv 直连三服务（经端口暴露覆盖），跑端到端驱动 + 不变量断言（多数表决）。
echo "[live-e2e] step4 运行 live e2e smoke 驱动"
set +e
PYTHONPATH=backend .venv/bin/python -m evaluation.live_e2e.smoke \
  --scenario evaluation/live_e2e/scenarios/disk_badtrack.json \
  --mode "$MODE" --attempts "$ATTEMPTS" \
  --case-base "http://localhost:8001" \
  --conv-base "http://localhost:8002" \
  --agent-base "http://localhost:8005" \
  --internal-token "$INTERNAL_API_TOKEN" \
  --dump-dir "$DUMP_DIR"
RC=$?
set -e

collect_logs
if [ "$RC" -ne 0 ]; then
  echo "[live-e2e] 🔴 smoke 失败（RC=$RC）。缝隙定位请看: $DUMP_DIR/attempt-*/ 与 $DUMP_DIR/logs/"
else
  echo "[live-e2e] ✅ smoke 通过"
fi
exit "$RC"
