# LLM 配置结构重构：单一配置源

## 日期
2026-06-09

## 问题
htp-agent 的 LLM 配置（base_url / model / api_key）在 `assistantRegistryJson` JSON 字符串中硬编码，与 `config.llmBaseUrl` + `secrets.llmApiKey` 重复。切换模型时需要改多个地方，容易遗漏。

## 方案
新增顶级 `llm` 配置节作为 LLM Provider 唯一配置源，Helm 模板自动生成 `assistantRegistryJson`。

### 新增配置
```yaml
llm:
  baseUrl: "https://coding.dashscope.aliyuncs.com/v1"
  model: "glm-5"
  apiCompletionsPath: "/chat/completions"
  readTimeoutSec: 120.0
```

### 变更文件
- `deploy/helm/hci-platform/values.yaml` — 新增 `llm` 配置节
- `deploy/helm/hci-platform/templates/_helpers.tpl` — 新增 `hci.assistantRegistry` 模板函数
- `deploy/helm/hci-platform/templates/configmap.yaml` — 所有 LLM env 改从 `llm.*` 读取

### 环境 values.yaml 变更
切换模型时只需修改 `llm.baseUrl` 和 `llm.model`，无需手动维护 JSON 字符串。

## 相关 PR
- hci-troubleshoot-platform: #419
- hci-platform-env: #18
