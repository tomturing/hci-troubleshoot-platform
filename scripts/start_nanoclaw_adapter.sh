#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

: "${ADAPTER_CMD_JSON:=}"
: "${ADAPTER_CMD:=}"

if [[ -z "$ADAPTER_CMD_JSON" && -z "$ADAPTER_CMD" ]]; then
  cat >&2 <<MSG
[ERROR] NanoClaw 当前没有稳定 one-shot CLI，需你提供执行命令模板。
请设置其一：
  export ADAPTER_CMD_JSON='["your_runner","{prompt}","{session}"]'
或
  export ADAPTER_CMD='your_runner --prompt "{prompt}" --session "{session}"'
然后重试。
MSG
  exit 1
fi

export ADAPTER_HOST="${ADAPTER_HOST:-0.0.0.0}"
export ADAPTER_PORT="${ADAPTER_PORT:-43103}"
export ADAPTER_MODEL="${ADAPTER_MODEL:-nanoclaw}"
export ADAPTER_BEARER_TOKEN="${ADAPTER_BEARER_TOKEN:-nc_dev_token}"

exec python3 "$ROOT_DIR/adapters/cli_openai_adapter/server.py"
