#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:43101}"
TOKEN="${TOKEN:-pc_dev_token}"
MODEL="${MODEL:-picoclaw}"
MSG="${MSG:-请直接回答：1+1等于几？只输出数字。}"

curl -sS "$BASE_URL/v1/chat/completions" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"$MSG\"}],\"stream\":false,\"user\":\"adapter-probe\"}"
