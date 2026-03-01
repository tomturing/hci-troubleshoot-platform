#!/usr/bin/env sh
set -eu

PICO_HOME="${PICO_HOME:-/root/.picoclaw}"
WORKSPACE_DIR="${PICO_WORKSPACE_DIR:-$PICO_HOME/workspace}"
UPSTREAM_BASE_URL="${PICOCLAW_UPSTREAM_BASE_URL:-http://openclaw:18789/v1}"
UPSTREAM_TOKEN="${PICOCLAW_UPSTREAM_TOKEN:-${OPENCLAW_GATEWAY_TOKEN:-hci-dev-openclaw-token}}"
MODEL_ALIAS="${PICOCLAW_MODEL_ALIAS:-openclaw-local}"
UPSTREAM_MODEL="${PICOCLAW_UPSTREAM_MODEL:-openclaw}"

mkdir -p "$PICO_HOME" "$WORKSPACE_DIR"

cat > "$PICO_HOME/config.json" <<JSON
{
  "agents": {
    "defaults": {
      "workspace": "$WORKSPACE_DIR",
      "restrict_to_workspace": true,
      "model_name": "$MODEL_ALIAS",
      "model": "$MODEL_ALIAS",
      "max_tokens": 8192,
      "max_tool_iterations": 12
    }
  },
  "model_list": [
    {
      "model_name": "$MODEL_ALIAS",
      "model": "openai/$UPSTREAM_MODEL",
      "api_base": "$UPSTREAM_BASE_URL",
      "api_key": "$UPSTREAM_TOKEN",
      "request_timeout": 120
    }
  ]
}
JSON

exec python /app/server.py
