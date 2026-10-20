# agent-service/eval-service 依赖修复

## 背景

PR 309 合并后，agent-service 和 eval-service 因缺少 opentelemetry 依赖无法启动。

## 问题

```
ModuleNotFoundError: No module named 'opentelemetry.exporter.otlp.proto.grpc'
```

## 修复

- agent-service/pyproject.toml 添加 `opentelemetry-exporter-otlp`、`opentelemetry-instrumentation-logging`
- eval-service/pyproject.toml 添加 `opentelemetry-exporter-otlp`、`opentelemetry-instrumentation-logging`
- Helm values.yaml 修正镜像 repository 名称（去掉 hci- 前缀）
- sync-env-repo-tags.sh 添加 agentService/evalService 到服务列表

## 关联 PR

- PR 309: agent-service + eval-service 服务拆分
- PR 311: 本修复
