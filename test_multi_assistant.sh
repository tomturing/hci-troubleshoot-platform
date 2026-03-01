#!/usr/bin/env bash
set -euo pipefail

GATEWAY="${GATEWAY:-http://localhost:8000}"
CLIENT_PREFIX="${CLIENT_PREFIX:-multi-e2e}"
MESSAGE="${MESSAGE:-请给出 Kubernetes 节点 NotReady 的排查步骤（分步骤）。}"
MESSAGES_RAW="${MESSAGES:-}"
SSE_TIMEOUT_SEC="${SSE_TIMEOUT_SEC:-90}"
ASSISTANTS_RAW="${ASSISTANTS:-openclaw zeroclaw}"
TRANSCRIPT_MAX_CHARS="${TRANSCRIPT_MAX_CHARS:-400}"
CLI_ASSISTANTS=()
CLI_MESSAGES=()
USER_MESSAGES=()

usage() {
  cat <<'EOF'
用法:
  ./test_multi_assistant.sh [--assistant <name>]... [--message <text>]... [assistant...]

说明:
  - 默认测试: openclaw zeroclaw
  - 可用 ASSISTANTS 环境变量覆盖（兼容旧方式）
  - 命令行参数优先级最高
  - 对话消息优先级: --message(可多次) > MESSAGES(用 || 分隔) > MESSAGE(单条)

示例:
  ./test_multi_assistant.sh --assistant openclaw
  ./test_multi_assistant.sh zeroclaw
  ./test_multi_assistant.sh openclaw zeroclaw
  ASSISTANTS="zeroclaw" ./test_multi_assistant.sh
  ./test_multi_assistant.sh -a zeroclaw -m "你好" -m "继续说两点建议"
  MESSAGES="第一问||第二问||第三问" ./test_multi_assistant.sh -a openclaw
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help)
      usage
      exit 0
      ;;
    -a|--assistant)
      shift
      if [[ $# -eq 0 ]]; then
        echo "[FATAL] --assistant 需要一个参数。"
        exit 1
      fi
      CLI_ASSISTANTS+=("$1")
      ;;
    --assistant=*)
      CLI_ASSISTANTS+=("${1#*=}")
      ;;
    -m|--message)
      shift
      if [[ $# -eq 0 ]]; then
        echo "[FATAL] --message 需要一个参数。"
        exit 1
      fi
      CLI_MESSAGES+=("$1")
      ;;
    --message=*)
      CLI_MESSAGES+=("${1#*=}")
      ;;
    --)
      shift
      while [[ $# -gt 0 ]]; do
        CLI_ASSISTANTS+=("$1")
        shift
      done
      break
      ;;
    -*)
      echo "[FATAL] 未知参数: $1"
      usage
      exit 1
      ;;
    *)
      CLI_ASSISTANTS+=("$1")
      ;;
  esac
  shift
done

if [[ "${#CLI_ASSISTANTS[@]}" -gt 0 ]]; then
  ASSISTANTS_RAW="${CLI_ASSISTANTS[*]}"
fi

if command -v python3 >/dev/null 2>&1; then
  PYBIN="python3"
elif command -v python >/dev/null 2>&1; then
  PYBIN="python"
else
  echo "[FATAL] 需要 python3/python 用于 JSON 解析。"
  exit 1
fi

if ! command -v curl >/dev/null 2>&1; then
  echo "[FATAL] 需要 curl。"
  exit 1
fi

if [[ "${#CLI_MESSAGES[@]}" -gt 0 ]]; then
  USER_MESSAGES=("${CLI_MESSAGES[@]}")
elif [[ -n "$MESSAGES_RAW" ]]; then
  mapfile -t USER_MESSAGES < <(printf '%s' "$MESSAGES_RAW" | "$PYBIN" -c '
import sys
raw = sys.stdin.read()
for item in raw.split("||"):
    t = item.strip()
    if t:
        print(t)
')
else
  USER_MESSAGES=("$MESSAGE")
fi

if [[ "${#USER_MESSAGES[@]}" -eq 0 ]]; then
  echo "[FATAL] 未提供有效对话消息。请设置 --message / MESSAGES / MESSAGE。"
  exit 1
fi

REQ_STATUS=""
REQ_BODY=""

request_json() {
  local method="$1"
  local url="$2"
  local payload="${3:-}"
  local tmp
  tmp="$(mktemp)"
  if [[ -n "$payload" ]]; then
    REQ_STATUS="$(curl -sS -o "$tmp" -w "%{http_code}" -X "$method" "$url" -H "Content-Type: application/json" -d "$payload")"
  else
    REQ_STATUS="$(curl -sS -o "$tmp" -w "%{http_code}" -X "$method" "$url")"
  fi
  REQ_BODY="$(cat "$tmp")"
  rm -f "$tmp"
}

json_field() {
  local json_input="$1"
  local key="$2"
  printf '%s' "$json_input" | "$PYBIN" -c '
import json
import sys

key = sys.argv[1]
try:
    data = json.load(sys.stdin)
except Exception:
    print("")
    raise SystemExit(0)

current = data
for part in key.split("."):
    if isinstance(current, dict) and part in current:
        current = current[part]
    else:
        print("")
        raise SystemExit(0)

if current is None:
    print("")
elif isinstance(current, (dict, list)):
    print(json.dumps(current, ensure_ascii=False))
else:
    print(current)
' "$key"
}

assistant_present() {
  local assistants_json="$1"
  local assistant_type="$2"
  printf '%s' "$assistants_json" | "$PYBIN" -c '
import json
import sys

target = sys.argv[1]
try:
    items = json.load(sys.stdin)
except Exception:
    print("0")
    raise SystemExit(0)

found = any(isinstance(item, dict) and item.get("type") == target for item in items)
print("1" if found else "0")
' "$assistant_type"
}

assistant_available() {
  local assistants_json="$1"
  local assistant_type="$2"
  printf '%s' "$assistants_json" | "$PYBIN" -c '
import json
import sys

target = sys.argv[1]
try:
    items = json.load(sys.stdin)
except Exception:
    print("0")
    raise SystemExit(0)

for item in items:
    if isinstance(item, dict) and item.get("type") == target:
        print("1" if item.get("available", True) else "0")
        raise SystemExit(0)

print("0")
' "$assistant_type"
}

assistant_msg_count() {
  local messages_json="$1"
  printf '%s' "$messages_json" | "$PYBIN" -c '
import json
import sys

try:
    items = json.load(sys.stdin)
except Exception:
    print("0")
    raise SystemExit(0)

count = 0
for item in items:
    if isinstance(item, dict) and item.get("role") == "assistant":
        content = item.get("content") or ""
        if str(content).strip():
            count += 1
print(count)
'
}

sse_assistant_text() {
  local sse_file="$1"
  "$PYBIN" -c '
import json
import sys

path = sys.argv[1]
buf = []
with open(path, "r", encoding="utf-8", errors="ignore") as f:
    for line in f:
        if not line.startswith("data: "):
            continue
        data = line[6:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            obj = json.loads(data)
        except Exception:
            # 兼容纯文本 data: <chunk> 事件
            if not data.startswith("{") and not data.startswith("["):
                buf.append(data)
            continue
        if not isinstance(obj, dict):
            continue
        choices = obj.get("choices")
        if not isinstance(choices, list) or not choices:
            # 兼容 {"content":"..."} / {"text":"..."} / {"chunk":"..."}
            for key in ("content", "text", "chunk"):
                val = obj.get(key)
                if isinstance(val, str) and val:
                    buf.append(val)
                    break
            else:
                delta = obj.get("delta")
                if isinstance(delta, dict):
                    content = delta.get("content")
                    if content:
                        buf.append(str(content))
                message = obj.get("message")
                if isinstance(message, dict):
                    content = message.get("content")
                    if content:
                        buf.append(str(content))
            continue
        first = choices[0] if isinstance(choices[0], dict) else {}
        delta = first.get("delta")
        if isinstance(delta, dict):
            content = delta.get("content")
            if content:
                buf.append(str(content))
                continue
        message = first.get("message")
        if isinstance(message, dict):
            content = message.get("content")
            if content:
                buf.append(str(content))
                continue
        # 兼容 choices[0].text
        text = first.get("text")
        if isinstance(text, str) and text:
            buf.append(text)

print("".join(buf))
' "$sse_file"
}

print_messages() {
  local messages_json="$1"
  local max_chars="$2"
  printf '%s' "$messages_json" | "$PYBIN" -c '
import json
import re
import sys

max_chars = int(sys.argv[1])
try:
    items = json.load(sys.stdin)
except Exception:
    print("  (invalid JSON)")
    raise SystemExit(0)

if not isinstance(items, list) or not items:
    print("  (empty)")
    raise SystemExit(0)

for item in items:
    if not isinstance(item, dict):
        continue
    role = str(item.get("role") or "unknown")
    content = str(item.get("content") or "")
    content = re.sub(r"\s+", " ", content).strip()
    if len(content) > max_chars:
        content = content[:max_chars] + "..."
    print(f"  - {role}: {content}")
' "$max_chars"
}

print_assistants() {
  local assistants_json="$1"
  printf '%s' "$assistants_json" | "$PYBIN" -c '
import json
import sys

try:
    items = json.load(sys.stdin)
except Exception:
    print("  (invalid JSON)")
    raise SystemExit(0)

if not isinstance(items, list) or not items:
    print("  (empty)")
    raise SystemExit(0)

for item in items:
    if not isinstance(item, dict):
        continue
    a_type = item.get("type", "unknown")
    name = item.get("display_name") or item.get("name") or a_type
    available = item.get("available", True)
    print(f"  - {a_type} ({name}), available={available}")
'
}

fail_reason() {
  local assistant="$1"
  local reason="$2"
  FAILED+=("${assistant}: ${reason}")
  echo "[FAIL][$assistant] ${reason}"
}

run_assistant_flow() {
  local assistant="$1"
  local now_ts case_payload case_id conv_id msg_payload sse_file sse_status msg_count
  local turn_idx user_msg assistant_reply

  now_ts="$(date +%s)"
  case_payload="$(cat <<EOF
{"client_id":"${CLIENT_PREFIX}-${assistant}-${now_ts}","title":"[${assistant}] E2E 联调","description":"验证 ${assistant} 助手链路","assistant_type":"${assistant}"}
EOF
)"

  request_json POST "${GATEWAY}/api/cases/" "$case_payload"
  if [[ "$REQ_STATUS" != "201" ]]; then
    fail_reason "$assistant" "创建工单失败 (HTTP ${REQ_STATUS})，body=${REQ_BODY}"
    return 1
  fi
  case_id="$(json_field "$REQ_BODY" "case_id")"
  if [[ -z "$case_id" ]]; then
    fail_reason "$assistant" "创建工单响应中缺少 case_id，body=${REQ_BODY}"
    return 1
  fi
  echo "[INFO][$assistant] case_id=$case_id"

  request_json PUT "${GATEWAY}/api/cases/${case_id}/confirm"
  if [[ "$REQ_STATUS" != "200" ]]; then
    fail_reason "$assistant" "确认工单失败 (HTTP ${REQ_STATUS})，body=${REQ_BODY}"
    return 1
  fi

  request_json POST "${GATEWAY}/api/conversations/?case_id=${case_id}&assistant_type=${assistant}"
  if [[ "$REQ_STATUS" != "201" ]]; then
    fail_reason "$assistant" "创建会话失败 (HTTP ${REQ_STATUS})，body=${REQ_BODY}"
    return 1
  fi
  conv_id="$(json_field "$REQ_BODY" "conversation_id")"
  if [[ -z "$conv_id" ]]; then
    fail_reason "$assistant" "创建会话响应中缺少 conversation_id，body=${REQ_BODY}"
    return 1
  fi
  echo "[INFO][$assistant] conversation_id=$conv_id"

  turn_idx=0
  for user_msg in "${USER_MESSAGES[@]}"; do
    turn_idx=$((turn_idx + 1))
    msg_payload="$(cat <<EOF
{"case_id":"${case_id}","role":"user","content":"${user_msg}"}
EOF
)"
    sse_file="$(mktemp)"
    sse_status="$(curl -sS -N --max-time "$SSE_TIMEOUT_SEC" -o "$sse_file" -w "%{http_code}" -X POST "${GATEWAY}/api/conversations/${conv_id}/message" -H "Content-Type: application/json" -d "$msg_payload")"
    if [[ "$sse_status" != "200" ]]; then
      rm -f "$sse_file"
      fail_reason "$assistant" "第${turn_idx}轮发送消息失败 (HTTP ${sse_status})"
      return 1
    fi

    if grep -q "^event: error" "$sse_file"; then
      local err_preview
      err_preview="$(head -n 8 "$sse_file" | tr '\n' ' ' | sed 's/[[:space:]]\+/ /g')"
      rm -f "$sse_file"
      fail_reason "$assistant" "第${turn_idx}轮 SSE 返回错误事件: ${err_preview}"
      return 1
    fi

    if grep -q "System Error" "$sse_file"; then
      local sys_preview
      sys_preview="$(head -n 8 "$sse_file" | tr '\n' ' ' | sed 's/[[:space:]]\+/ /g')"
      rm -f "$sse_file"
      fail_reason "$assistant" "第${turn_idx}轮 AI 返回系统错误: ${sys_preview}"
      return 1
    fi

    if ! grep -q "data: \[DONE\]" "$sse_file"; then
      local done_preview
      done_preview="$(tail -n 8 "$sse_file" | tr '\n' ' ' | sed 's/[[:space:]]\+/ /g')"
      rm -f "$sse_file"
      fail_reason "$assistant" "第${turn_idx}轮 SSE 未收到 [DONE]: ${done_preview}"
      return 1
    fi

    assistant_reply="$(sse_assistant_text "$sse_file" | tr '\n' ' ' | sed 's/[[:space:]]\+/ /g')"
    rm -f "$sse_file"

    if [[ -z "${assistant_reply}" ]]; then
      assistant_reply="<empty>"
    fi
    echo "[CHAT][$assistant][turn ${turn_idx}] user: ${user_msg}"
    echo "[CHAT][$assistant][turn ${turn_idx}] assistant: ${assistant_reply}"
  done

  sleep 1
  request_json GET "${GATEWAY}/api/conversations/${conv_id}/messages"
  if [[ "$REQ_STATUS" != "200" ]]; then
    fail_reason "$assistant" "查询消息历史失败 (HTTP ${REQ_STATUS})，body=${REQ_BODY}"
    return 1
  fi

  msg_count="$(assistant_msg_count "$REQ_BODY")"
  if [[ "${msg_count}" -lt "${#USER_MESSAGES[@]}" ]]; then
    fail_reason "$assistant" "消息历史中 assistant 回复数不足（期望>=${#USER_MESSAGES[@]}，实际=${msg_count}）"
    return 1
  fi
  echo "[TRANSCRIPT][$assistant]"
  print_messages "$REQ_BODY" "$TRANSCRIPT_MAX_CHARS"

  request_json PUT "${GATEWAY}/api/cases/${case_id}/close"
  if [[ "$REQ_STATUS" != "200" ]]; then
    echo "[WARN][$assistant] 关闭工单失败 (HTTP ${REQ_STATUS})，不影响链路判定。"
  fi

  PASSED+=("$assistant")
  echo "[PASS][$assistant] 链路验证通过。"
  return 0
}

echo "=== 多助手端到端联调 (gateway=${GATEWAY}) ==="
echo "目标助手: ${ASSISTANTS_RAW}"
echo "消息轮数: ${#USER_MESSAGES[@]}"

request_json GET "${GATEWAY}/api/assistants/"
if [[ "$REQ_STATUS" != "200" ]]; then
  echo "[FATAL] 查询 /api/assistants 失败 (HTTP ${REQ_STATUS})"
  echo "body=${REQ_BODY}"
  exit 1
fi

ASSISTANTS_JSON="$REQ_BODY"
echo "当前平台注册助手："
print_assistants "$ASSISTANTS_JSON"
echo ""

PASSED=()
FAILED=()
IFS=' ' read -r -a TARGET_ASSISTANTS <<< "${ASSISTANTS_RAW//,/ }"

for assistant in "${TARGET_ASSISTANTS[@]}"; do
  [[ -z "$assistant" ]] && continue
  echo "=== 验证助手: ${assistant} ==="

  if [[ "$(assistant_present "$ASSISTANTS_JSON" "$assistant")" != "1" ]]; then
    fail_reason "$assistant" "未出现在 /api/assistants 返回结果中（请先接入 ASSISTANT_REGISTRY_JSON）"
    echo ""
    continue
  fi

  if [[ "$(assistant_available "$ASSISTANTS_JSON" "$assistant")" != "1" ]]; then
    echo "[WARN][$assistant] /api/assistants 标记 available=false，继续尝试链路验证。"
  fi

  run_assistant_flow "$assistant" || true
  echo ""
done

echo "=== 汇总 ==="
if [[ "${#PASSED[@]}" -gt 0 ]]; then
  echo "PASS: ${PASSED[*]}"
else
  echo "PASS: (none)"
fi

if [[ "${#FAILED[@]}" -gt 0 ]]; then
  echo "FAIL:"
  for item in "${FAILED[@]}"; do
    echo "  - ${item}"
  done
  exit 1
fi

echo "全部目标助手链路验证通过。"
