#!/usr/bin/env sh
set -eu

NB_HOME="${NB_HOME:-/root/.nanobot}"
WORKSPACE_DIR="${NANOBOT_WORKSPACE_DIR:-$NB_HOME/workspace}"
UPSTREAM_BASE_URL="${NANOBOT_UPSTREAM_BASE_URL:-http://openclaw:18789/v1}"
UPSTREAM_TOKEN="${NANOBOT_UPSTREAM_TOKEN:-${OPENCLAW_GATEWAY_TOKEN:-hci-dev-openclaw-token}}"
UPSTREAM_MODEL="${NANOBOT_UPSTREAM_MODEL:-openclaw}"

mkdir -p "$NB_HOME" "$WORKSPACE_DIR"

cat > "$NB_HOME/config.json" <<JSON
{
  "agents": {
    "defaults": {
      "workspace": "$WORKSPACE_DIR",
      "model": "$UPSTREAM_MODEL",
      "provider": "custom",
      "max_tokens": 8192,
      "max_tool_iterations": 12
    }
  },
  "providers": {
    "custom": {
      "api_key": "$UPSTREAM_TOKEN",
      "api_base": "$UPSTREAM_BASE_URL"
    }
  }
}
JSON

exec python /app/server.py
