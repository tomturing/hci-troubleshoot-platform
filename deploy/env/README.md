# =============================================================================
# HCI 智能排障平台 - Helm 环境配置说明
# =============================================================================
#
# 目录结构：
#   deploy/env/
#     platform.env          # 所有非敏感变量基准（提交 git）
#     secrets.env.example   # 敏感变量模板（提交 git）
#     secrets.env           # 本地真实敏感值（不提交 git，.gitignore 屏蔽）
#
#   deploy/helm/hci-platform/
#     values.yaml                    # Helm 默认值（提交 git）
#     values-prod.yaml               # 生产环境增强配置（提交 git）
#
#   .local/
#     values-prod.override.yaml      # 个人本地覆盖（不提交 git）
#
# =============================================================================
# 部署命令示例
# =============================================================================
#
# 【本地开发 / WSL】
#   helm upgrade --install hci-platform deploy/helm/hci-platform \
#     -n hci-troubleshoot \
#     -f deploy/helm/hci-platform/values.yaml \
#     -f .local/values-prod.override.yaml
#
# 【CI / 生产（通过 secrets.env）】
#   source deploy/env/secrets.env  # 加载密钥到环境变量
#   helm upgrade --install hci-platform deploy/helm/hci-platform \
#     -n hci-troubleshoot --create-namespace \
#     -f deploy/helm/hci-platform/values.yaml \
#     -f deploy/helm/hci-platform/values-prod.yaml \
#     --set secrets.postgresPassword="$HCI_SECRET_POSTGRES_PASSWORD" \
#     --set secrets.openclawToken="$HCI_SECRET_OPENCLAW_TOKEN" \
#     --set secrets.zaiApiKey="$HCI_SECRET_ZAI_API_KEY" \
#     --set secrets.grafanaAdminPassword="$HCI_SECRET_GRAFANA_ADMIN_PASSWORD"
#
# =============================================================================
# 配置变量与 Helm values.yaml 字段的对应关系
# =============================================================================
#
# deploy/env/platform.env        →  Helm values.yaml 字段
# ─────────────────────────────────────────────────────────────────
# HCI_DOMAIN                     →  global.domain
# HCI_INGRESS_ENTRYPOINT         →  ingress.annotations (traefik entrypoints)
# HCI_K8S_NAMESPACE              →  global.namespace
# HCI_K8S_CONTAINER_RUNTIME      →  observability.promtail.containerRuntime
# HCI_IMAGE_REGISTRY             →  global.imageRegistry
# HCI_IMAGE_PULL_POLICY          →  global.imagePullPolicy
# HCI_POSTGRES_DB                →  config.postgresDb
# HCI_POSTGRES_USER              →  config.postgresUser
# HCI_REDIS_URL                  →  config.redisUrl
# HCI_LOG_LEVEL                  →  config.logLevel
# HCI_OTEL_ENDPOINT              →  config.otelEndpoint
# HCI_WARM_POOL_SIZE             →  config.warmPoolSize
# HCI_MAX_POOL_SIZE              →  config.maxPoolSize
# HCI_GRAFANA_DOMAIN             →  observability.grafana.domain
# HCI_GRAFANA_ROOT_URL           →  observability.grafana.rootUrl
# HCI_TEMPO_MEMORY_LIMIT         →  observability.tempo.resources.limits.memory
#
# deploy/env/secrets.env         →  Helm values.yaml secrets 字段
# ─────────────────────────────────────────────────────────────────
# HCI_SECRET_POSTGRES_PASSWORD   →  secrets.postgresPassword
# HCI_SECRET_OPENCLAW_TOKEN      →  secrets.openclawToken
# HCI_SECRET_ZAI_API_KEY         →  secrets.zaiApiKey
# HCI_SECRET_GRAFANA_ADMIN_PASSWORD → secrets.grafanaAdminPassword
