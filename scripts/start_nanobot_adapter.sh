#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NANOBOT_BIN="${NANOBOT_BIN:-nanobot}"

if ! command -v "$NANOBOT_BIN" >/dev/null 2>&1; then
  cat >&2 <<MSG
[ERROR] 未找到 nanobot 命令: $NANOBOT_BIN
请先安装/加入 PATH，例如：
  cd /mnt/d/AIBot/nanobot && pip install -e .
或设置：
  export NANOBOT_BIN=/path/to/nanobot
MSG
  exit 1
fi

export ADAPTER_HOST="${ADAPTER_HOST:-0.0.0.0}"
export ADAPTER_PORT="${ADAPTER_PORT:-43102}"
export ADAPTER_MODEL="${ADAPTER_MODEL:-nanobot}"
export ADAPTER_BEARER_TOKEN="${ADAPTER_BEARER_TOKEN:-nb_dev_token}"
export ADAPTER_CMD_JSON="${ADAPTER_CMD_JSON:-[\"$NANOBOT_BIN\",\"agent\",\"--message\",\"{prompt}\",\"--session\",\"{session}\",\"--no-markdown\",\"--no-logs\"]}"

exec python3 "$ROOT_DIR/adapters/cli_openai_adapter/server.py"
