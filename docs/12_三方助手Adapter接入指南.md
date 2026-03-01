# 三方助手 Adapter 接入指南（NanoBot / NanoClaw / PicoClaw）

更新时间：2026-02-28

## 1. 目标

现有平台统一协议是：

- `POST /v1/chat/completions`
- `Authorization: Bearer <token>`
- `SSE` 流式（`stream=true`）

`nanobot` / `nanoclaw` / `picoclaw` 不直接提供该协议时，可通过 CLI Adapter 接入。

## 2. 适配器位置

- 代码：`adapters/cli_openai_adapter/server.py`
- 一键启动脚本：
  - `scripts/start_picoclaw_adapter.sh`
  - `scripts/start_nanobot_adapter.sh`
  - `scripts/start_nanoclaw_adapter.sh`

## 3. 启动 Adapter

### 3.1 PicoClaw（优先打样）

先确保有可执行文件：

```bash
cd /mnt/d/AIBot/picoclaw
make build
```

启动：

```bash
cd /mnt/d/AIBot/hci-troubleshoot-platform
ADAPTER_BEARER_TOKEN=pc_dev_token \
ADAPTER_PORT=43101 \
bash scripts/start_picoclaw_adapter.sh
```

### 3.2 NanoBot

先安装 CLI：

```bash
cd /mnt/d/AIBot/nanobot
pip install -e .
```

启动：

```bash
cd /mnt/d/AIBot/hci-troubleshoot-platform
ADAPTER_BEARER_TOKEN=nb_dev_token \
ADAPTER_PORT=43102 \
bash scripts/start_nanobot_adapter.sh
```

### 3.3 NanoClaw

NanoClaw 当前没有稳定 one-shot CLI，需先提供 runner 命令模板：

```bash
cd /mnt/d/AIBot/hci-troubleshoot-platform
export ADAPTER_CMD='your_nanoclaw_runner --prompt "{prompt}" --session "{session}"'
ADAPTER_BEARER_TOKEN=nc_dev_token ADAPTER_PORT=43103 bash scripts/start_nanoclaw_adapter.sh
```

## 4. 注册到统一配置（ASSISTANT_REGISTRY_JSON）

把以下片段并入 `.env` 的 `ASSISTANT_REGISTRY_JSON`：

```json
{
  "picoclaw": {
    "name": "PicoClaw",
    "description": "PicoClaw via CLI Adapter",
    "image": "cli-openai-adapter:local",
    "port": 43101,
    "warm_pool_size": 0,
    "max_pool_size": 3,
    "enabled": true,
    "base_url": "http://host.docker.internal:43101",
    "gateway_token": "pc_dev_token",
    "model": "picoclaw",
    "labels": {"app": "picoclaw-adapter", "assistant-type": "picoclaw"}
  },
  "nanobot": {
    "name": "NanoBot",
    "description": "NanoBot via CLI Adapter",
    "image": "cli-openai-adapter:local",
    "port": 43102,
    "warm_pool_size": 0,
    "max_pool_size": 3,
    "enabled": true,
    "base_url": "http://host.docker.internal:43102",
    "gateway_token": "nb_dev_token",
    "model": "nanobot",
    "labels": {"app": "nanobot-adapter", "assistant-type": "nanobot"}
  },
  "nanoclaw": {
    "name": "NanoClaw",
    "description": "NanoClaw via CLI Adapter",
    "image": "cli-openai-adapter:local",
    "port": 43103,
    "warm_pool_size": 0,
    "max_pool_size": 3,
    "enabled": true,
    "base_url": "http://host.docker.internal:43103",
    "gateway_token": "nc_dev_token",
    "model": "nanoclaw",
    "labels": {"app": "nanoclaw-adapter", "assistant-type": "nanoclaw"}
  }
}
```

## 5. 重启平台服务

```bash
cd /mnt/d/AIBot/hci-troubleshoot-platform
docker compose -f deploy/docker/docker-compose.yml restart api-gateway conversation-service scheduler-service
```

## 6. 验证

查看助手列表：

```bash
curl -sS http://localhost:8000/api/assistants/
```

按助手联调：

```bash
bash test_multi_assistant.sh --assistant picoclaw -m "请直接回答：1+1="
bash test_multi_assistant.sh --assistant nanobot -m "请直接回答：1+1="
```

## 7. 关键说明

- Adapter 支持 `stream=true`，会把 CLI 整体结果切成 SSE chunk 返回。
- 会话连续性依赖 `user` 字段（平台已按 `case_id` 透传）。
- `nanoclaw` 目前建议先做 runner（脚本/桥接进程）再接 Adapter。

