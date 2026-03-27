#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:43101}"
TOKEN="${TOKEN:-pc_dev_token}"
MODEL="${MODEL:-picoclaw}"
MSG="${MSG:-请直接回答：1+1等于几？只输出数字。}"

if command -v python3 >/dev/null 2>&1; then
  PYBIN="python3"
elif command -v python >/dev/null 2>&1; then
  PYBIN="python"
else
  echo "[FATAL] 需要 python3/python 进行 JSON 序列化。"
  exit 1
fi

PAYLOAD="$("$PYBIN" -c '
import json
import sys
print(json.dumps({
    "model": sys.argv[1],
    "messages": [{"role": "user", "content": sys.argv[2]}],
    "stream": False,
    "user": "adapter-probe",
}, ensure_ascii=False))
' "$MODEL" "$MSG")"

curl -sS "$BASE_URL/v1/chat/completions" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d "$PAYLOAD"
