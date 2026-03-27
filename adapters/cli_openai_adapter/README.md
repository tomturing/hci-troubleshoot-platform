# CLI OpenAI Adapter

把任意本地命令行助手包装成 OpenAI 兼容接口：

- `POST /v1/chat/completions`
- `GET /v1/models`
- `GET /health`

## 1. 启动

### 本地直接运行

```bash
cd /path/to/AIBot/hci-troubleshoot-platform
ADAPTER_PORT=43101 \
ADAPTER_MODEL=picoclaw \
ADAPTER_BEARER_TOKEN=pc_dev_token \
ADAPTER_CMD_JSON='["picoclaw","agent","--message","{prompt}","--session","{session}"]' \
python3 adapters/cli_openai_adapter/server.py
```

### Docker 运行

```bash
cd /path/to/AIBot/hci-troubleshoot-platform

docker build -t cli-openai-adapter:local -f adapters/cli_openai_adapter/Dockerfile /path/to/AIBot/hci-troubleshoot-platform

docker run --rm -p 43101:43101 \
  -e ADAPTER_MODEL=picoclaw \
  -e ADAPTER_BEARER_TOKEN=pc_dev_token \
  -e ADAPTER_CMD_JSON='["picoclaw","agent","--message","{prompt}","--session","{session}"]' \
  cli-openai-adapter:local
```

## 2. 请求示例

```bash
curl -sS http://localhost:43101/v1/chat/completions \
  -H 'Authorization: Bearer pc_dev_token' \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "picoclaw",
    "messages": [{"role": "user", "content": "请回答 1+1"}],
    "stream": false,
    "user": "case-123"
  }'
```

## 3. 环境变量

- `ADAPTER_HOST` / `ADAPTER_PORT`: 监听地址，默认 `0.0.0.0:43101`
- `ADAPTER_BEARER_TOKEN`: 鉴权 token（为空则不校验）
- `ADAPTER_MODEL`: 默认模型名，默认 `cli-adapter`
- `ADAPTER_CMD_JSON`: 命令模板（JSON 数组，推荐）
- `ADAPTER_CMD`: 命令模板（shell 字符串）
- `ADAPTER_TIMEOUT_SEC`: 命令超时秒数，默认 `120`
- `ADAPTER_PROMPT_MODE`: `last_user`（默认）或 `transcript`
- `ADAPTER_MAX_PROMPT_CHARS`: prompt 截断长度，默认 `12000`
- `ADAPTER_STREAM_CHUNK_CHARS`: SSE 分块大小，默认 `64`
- `ADAPTER_RESPONSE_REGEX`: 输出提取正则（可选）
- `ADAPTER_RESPONSE_PREFIX`: 响应行前缀裁剪（例如 `🦞 `）

## 4. 占位符

`ADAPTER_CMD_JSON` / `ADAPTER_CMD` 支持占位符：

- `{prompt}`: 从 `messages` 提取出的输入
- `{session}`: 使用 OpenAI `user` 字段
- `{model}`: 请求里的 `model`
