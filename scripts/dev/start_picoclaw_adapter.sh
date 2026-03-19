#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AIBOT_ROOT="${AIBOT_ROOT:-$(cd "$ROOT_DIR/.." && pwd)}"
PICOCLAW_BIN="${PICOCLAW_BIN:-$AIBOT_ROOT/picoclaw/picoclaw}"

if [[ ! -x "$PICOCLAW_BIN" ]]; then
  cat >&2 <<MSG
[ERROR] 未找到可执行 picoclaw: $PICOCLAW_BIN
请先构建：
  cd $AIBOT_ROOT/picoclaw && make build
然后设置：
  export PICOCLAW_BIN=$AIBOT_ROOT/picoclaw/picoclaw
MSG
  exit 1
fi

export ADAPTER_HOST="${ADAPTER_HOST:-0.0.0.0}"
export ADAPTER_PORT="${ADAPTER_PORT:-43101}"
export ADAPTER_MODEL="${ADAPTER_MODEL:-picoclaw}"
export ADAPTER_BEARER_TOKEN="${ADAPTER_BEARER_TOKEN:-pc_dev_token}"
export ADAPTER_RESPONSE_PREFIX="${ADAPTER_RESPONSE_PREFIX:-🦞 }"
export ADAPTER_CMD_JSON="${ADAPTER_CMD_JSON:-[\"$PICOCLAW_BIN\",\"agent\",\"--message\",\"{prompt}\",\"--session\",\"{session}\"]}"

exec python3 "$ROOT_DIR/adapters/cli_openai_adapter/server.py"
