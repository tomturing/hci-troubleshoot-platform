# 配置重构：移除 OpenClaw/ProductionClaw/LearningClaw 配置

**日期**: 2026-05-21
**类型**: refactor
**影响范围**: 后端配置、Helm Chart

## 变更背景

平台已迁移至统一使用 dashscope 网关调用 GLM-5 模型，不再需要本地 OpenClaw/ProductionClaw/LearningClaw Pod 池。此次重构清理了所有废弃配置。

## 主要变更

### 1. Helm 模板删除

删除以下已废弃的模板文件：
- `templates/openclaw-service.yaml` - OpenClaw Deployment
- `templates/openclaw-configmap.yaml` - OpenClaw 配置
- `templates/learningclaw.yaml` - LearningClaw Deployment
- `templates/learningclaw-init-configmap.yaml` - LearningClaw 初始化配置
- `templates/productionclaw-init-configmap.yaml` - ProductionClaw 初始化配置
- `values-dev.yaml` - 开发环境配置（已废弃）

### 2. 环境变量重命名

| 旧变量名 | 新变量名 | 说明 |
|---------|---------|------|
| `OPENCLAW_BASE_URL` | `LLM_BASE_URL` | LLM API 地址 |
| `OPENCLAW_API_KEY` | `LLM_API_KEY` | LLM API 密钥 |
| `OPENCLAW_GATEWAY_TOKEN` | (移除) | 不再需要内部网关鉴权 |
| `OPENCLAW_DEFAULT_MODEL` | `LLM_DEFAULT_MODEL` | 默认模型名 |
| `GLM_MODEL` (glm-4-flash) | `GLM_MODEL` (glm-5) | 默认模型升级 |

### 3. 移除的配置项

#### Secret
- `OPENCLAW_GATEWAY_TOKEN`
- `OPENCLAW_API_KEY`
- `SCP_API_KEY`
- `HCI_SSH_PASSWORD`

#### Deployment 环境变量
- `SCP_BASE_URL`
- `SCP_API_KEY`
- `HCI_SSH_USER`
- `HCI_SSH_KEY_PATH`
- `HCI_SSH_PASSWORD`

### 4. 助手注册表更新

默认助手注册表从：
```json
{
  "openclaw": {...},
  "productionclaw": {...},
  "learningclaw": {...}
}
```

更新为：
```json
{
  "htp-agent": {
    "base_url": "https://coding.dashscope.aliyuncs.com/v1",
    "model": "glm-5",
    "is_default": true
  },
  "ops-agent": {
    "base_url": "http://ops-agent-service:8006"
  },
  "pai-agent": {
    "base_url": "http://conversation-service:8002"
  }
}
```

## 后端服务变更

### agent-service
- `config.py`: 使用 `LLM_BASE_URL`/`LLM_API_KEY`
- `main.py`: 适配新配置
- `adapters/*`: 更新环境变量引用
- `glm_client.py`: 默认模型改为 glm-5

### conversation-service
- `config.py`: 使用 `LLM_BASE_URL`/`LLM_API_KEY`
- `main.py`: 适配新配置
- `conversation_service.py`: 更新 fallback endpoint

### scheduler-service
- `config.py`: 更新助手注册表默认值，移除 `OPENCLAW_IMAGE`
- `k8s_client.py`: 简化 Pod 创建逻辑（当前架构不再使用动态 Pod）

### kb-service
- `routes/classify.py`: 使用 `LLM_BASE_URL`/`LLM_API_KEY`

## 迁移指南

### 对于本地开发

1. 更新 `.env` 文件：
   ```bash
   # 旧配置
   OPENCLAW_BASE_URL=http://host.docker.internal:18790
   OPENCLAW_API_KEY=xxx

   # 新配置
   LLM_BASE_URL=https://coding.dashscope.aliyuncs.com/v1
   LLM_API_KEY=your_dashscope_api_key
   ```

2. 更新模型名称：
   ```bash
   GLM_MODEL=glm-5  # 从 glm-4-flash 升级
   ```

### 对于 Helm 部署

1. 更新 values override 文件：
   ```yaml
   secrets:
     llmApiKey: "your_dashscope_api_key"

   config:
     llmBaseUrl: "https://coding.dashscope.aliyuncs.com/v1"
     llmDefaultModel: "glm-5"
   ```

2. 删除废弃配置：
   ```yaml
   # 不再需要
   secrets:
     openclawToken: "xxx"
     zaiApiKey: "xxx"

   openclaw:
     enabled: true  # 移除

   learningclaw:
     enabled: true  # 移除
   ```

## 验证

- [x] 所有 `OPENCLAW_*` 引用已替换为 `LLM_*`
- [x] 所有 `glm-4-flash` 已替换为 `glm-5`
- [x] Helm 模板无废弃文件
- [x] values.schema.json 已更新
- [x] 示例配置文件已更新
