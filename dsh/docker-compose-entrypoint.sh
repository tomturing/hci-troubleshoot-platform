#!/bin/sh
# Docker Compose 专用启动包装：把 .env 中的 LLM 配置生成成 DSH profile patch，
# 随后复用生产镜像的统一入口。生产 Helm 由 ConfigMap 提供同等 patch。
set -eu

PROFILE="${DSH_PROFILE:-web}"
PATCH_FILE="${DSH_COMPOSE_PATCH_FILE:-${DSH_HOME:-${HOME}/.dsh}/deploy/cordis.compose.patch.yml}"
mkdir -p "$(dirname "$PATCH_FILE")"

PROVIDER="${DSH_LLM_PROVIDER_ID:-aliyun-maas}"
API="${DSH_LLM_API:-openai-completions}"
BASE_URL="${DSH_LLM_BASE_URL:-https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1}"
MODEL="${DSH_LLM_MODEL:-qwen3.8-flash}"

# 这些值来自本地 .env，均作为 YAML 双引号字符串写入；API Key 只通过
# apiKeyEnv 引用，不把密钥写进 profile 文件。
cat > "$PATCH_FILE" <<EOF
- id: llm-pi-ai
  config:
    providers:
      "${PROVIDER}":
        apiKeyEnv: DSH_QWEN_API_KEY
        api: "${API}"
        baseURL: "${BASE_URL}"
        compat:
          supportsDeveloperRole: false
          thinkingFormat: qwen
        reasoning: 'off'
        models:
          - id: "${MODEL}"
            reasoningEfforts:
              'off':
              'high': 'high'
- id: agent-default-model
  config:
    provider: "${PROVIDER}"
    model: "${MODEL}"
EOF

# Compose 的普通与离线实例都通过此开关追加 workflow；未设置时保持原有单 Agent 配置。
if [ "${DSH_COMPOSE_WORKFLOW_ENABLED:-false}" = "true" ]; then
    cat >> "$PATCH_FILE" <<'EOF'
- id: workflow-ptc
  name: '@deepseek-ai/dsh-workflow-ptc'
  disabled: false
  config:
    provider: fork-inflight
    maxConcurrentAgents: 3
    maxTotalAgents: 20
- id: tool-workflow
  name: '@deepseek-ai/dsh-tool-workflow'
  disabled: false
EOF
fi

export DSH_RUNTIME_PATCH="$PATCH_FILE"
exec /usr/local/bin/docker-entrypoint.sh
