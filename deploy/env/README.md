# HCI 智能排障平台 — Helm 环境配置说明

## 概述

`deploy/env/` 存放本地开发和应急部署用的环境变量配置。

**日常生产发布请使用 GitOps 流程**（CI 构建镜像 → 自动 PR 到环境仓库 → ArgoCD 同步），
本目录下的 `secrets.env` 仅用于本地调试和应急 Helm 直接部署。

## 目录结构

```
deploy/env/
  platform.env          # 所有非敏感变量基准（提交 git）
  secrets.env.example   # 敏感变量模板（提交 git）
  secrets.env           # 本地真实敏感值（不提交 git，.gitignore 屏蔽）

deploy/helm/
  hci-platform/         # 业务服务 Helm Chart
  hci-platform-infra/   # 集群级资源（StorageClass + ClusterRole）
  hci-platform-data/    # 数据层（PostgreSQL + Redis）
  hci-platform-obs/     # 可观测性栈（Loki + Tempo + Grafana + Prometheus）
```

## 应急 Helm 直接部署（仅在无法通过 ArgoCD 时使用）

```bash
# 加载密钥
source deploy/env/secrets.env

# 业务服务
helm upgrade --install hci-platform deploy/helm/hci-platform \
  -n hci \
  -f deploy/helm/hci-platform/values.yaml \
  --set global.imageTag="$IMAGE_TAG" \
  --set secrets.postgresPassword="$HCI_SECRET_POSTGRES_PASSWORD" \
  --set secrets.openclawToken="$HCI_SECRET_OPENCLAW_TOKEN" \
  --set secrets.zaiApiKey="$HCI_SECRET_ZAI_API_KEY"
```

## 配置变量与 Helm values.yaml 字段对应关系

```
deploy/env/platform.env        →  Helm values.yaml 字段
─────────────────────────────────────────────────────────────────
HCI_DOMAIN                     →  global.domain
HCI_INGRESS_ENTRYPOINT         →  ingress.annotations
HCI_K8S_NAMESPACE              →  global.namespace
HCI_K8S_CONTAINER_RUNTIME      →  observability.promtail.containerRuntime
HCI_IMAGE_REGISTRY             →  global.imageRegistry
HCI_IMAGE_PULL_POLICY          →  global.imagePullPolicy
HCI_POSTGRES_DB                →  config.postgresDb
HCI_POSTGRES_USER              →  config.postgresUser
HCI_REDIS_URL                  →  config.redisUrl
HCI_LOG_LEVEL                  →  config.logLevel
HCI_OTEL_ENDPOINT              →  config.otelEndpoint
HCI_WARM_POOL_SIZE             →  config.warmPoolSize
HCI_MAX_POOL_SIZE              →  config.maxPoolSize
HCI_GRAFANA_DOMAIN             →  observability.grafana.domain
```
