# HCI 智能排障平台 - K8s 部署设计文档

## 文档信息
- **版本**: 1.0
- **作者**: Claude
- **日期**: 2026-02-15
- **状态**: 设计完成，待实施
- **前置文档**: [01_架构设计.md](01_架构设计.md), [04_可观测性设计.md](04_可观测性设计.md)

---

## 1. 设计背景与选型

### 1.1 当前部署现状

| 维度 | 现状 |
|------|------|
| 编排方式 | Docker Compose（开发 + 可观测性两个 compose 文件） |
| 运行环境 | Ubuntu 24.04 WSL2, 8 CPU / 7.6 GB RAM / 1 TB Disk |
| 容器数量 | 12 个（8 业务 + 4 可观测性） |
| 内存占用 | ~710 MiB（全部容器） |
| 存在问题 | 无滚动更新、无自愈、无资源限制、手动扩缩容 |

### 1.2 K3s 选型理由

| 方案 | 二进制体积 | 最低内存 | 内置组件 | 适用场景 |
|------|-----------|---------|---------|---------|
| **K3s** ✅ | ~50 MB | 512 MB | Traefik + CoreDNS + local-path-provisioner | 边缘/资源受限/单节点 |
| minikube | ~70 MB | 2 GB | 需额外安装 | 纯开发测试 |
| K8s (kubeadm) | ~300 MB | 2 GB | 无，全部手动 | 生产集群 |

**选择 K3s 的核心原因：**

1. **资源约束** — 总共 7.6 GB RAM，K3s 自身仅占 ~40 MiB（CoreDNS 12 MiB + metrics-server 20 MiB + local-path-provisioner 8 MiB），留给业务容器足够空间
2. **内置 Traefik** — 可直接用作 Ingress Controller，无需额外安装 Nginx Ingress
3. **内置 local-path-provisioner** — 自动管理 PVC，无需 NFS/Ceph
4. **单二进制** — 安装简单 (`curl -sfL https://get.k3s.io | sh -`)，已完成安装 v1.34.3+k3s1
5. **兼容标准 K8s API** — Helm Chart / kubectl 完全兼容，后续可平滑迁移到生产 K8s

### 1.3 当前 K3s 状态

```
$ k3s --version
k3s version v1.34.3+k3s1

$ sudo k3s kubectl get nodes
NAME   STATUS   ROLES           AGE   VERSION
gs     Ready    control-plane   24d   v1.34.3+k3s1
```

K3s 系统 Pod 资源占用：
| Pod | CPU | 内存 |
|-----|-----|------|
| coredns | 0.33% | 12.73 MiB |
| metrics-server | 0.55% | 20.67 MiB |
| local-path-provisioner | 0% | 7.82 MiB |
| **合计** | **~0.9%** | **~41 MiB** |

---

## 2. 命名空间策略

```
┌─────────────────────────────────────────────────────────┐
│                    K3s Cluster (gs)                       │
├─────────────────────────────────────────────────────────┤
│                                                           │
│  ┌─ kube-system ────────────────────────────────────┐    │
│  │ CoreDNS, metrics-server, local-path-provisioner  │    │
│  │ Traefik Ingress Controller                       │    │
│  └──────────────────────────────────────────────────┘    │
│                                                           │
│  ┌─ hci-troubleshoot ──────────────────────────────┐    │
│  │ 所有业务 Deployment / Service / ConfigMap        │    │
│  │ PostgreSQL StatefulSet + PVC                      │    │
│  │ Redis StatefulSet + PVC                           │    │
│  │ Ingress 规则                                      │    │
│  └──────────────────────────────────────────────────┘    │
│                                                           │
│  ┌─ hci-observability ─────────────────────────────┐    │
│  │ Loki / Tempo / Promtail / Grafana               │    │
│  │ OTel Collector (可选)                             │    │
│  └──────────────────────────────────────────────────┘    │
│                                                           │
└─────────────────────────────────────────────────────────┘
```

**设计原因：**
- `hci-troubleshoot` 与 `hci-observability` 分离，便于独立管理生命周期、RBAC 权限和资源配额
- 可观测组件可按需开关，不影响业务服务
- 两个 namespace 通过 K8s 内部 DNS 跨 namespace 通信（如 `tempo.hci-observability.svc.cluster.local:4317`）

---

## 3. 工作负载拓扑

### 3.1 全局拓扑图

```
                           ┌─────────────┐
                      :80  │   Traefik   │  K3s 内置 Ingress
                           │  (Ingress)  │
                           └──────┬──────┘
                                  │
              ┌───────────────────┼───────────────────┐
              │                   │                    │
        /api/*              /customer/*           /admin/*
              │                   │                    │
              ▼                   ▼                    ▼
    ┌─────────────────┐ ┌─────────────────┐  ┌─────────────────┐
    │  api-gateway    │ │  customer-ui    │  │  admin-ui       │
    │  Deployment(1)  │ │  Deployment(1)  │  │  Deployment(1)  │
    │  :8000          │ │  :80            │  │  :80            │
    └────────┬────────┘ └─────────────────┘  └─────────────────┘
             │
    ┌────────┼────────────────┐
    │        │                │
    ▼        ▼                ▼
┌────────┐ ┌─────────────┐ ┌──────────────┐
│ case   │ │conversation │ │ scheduler    │
│service │ │  service    │ │  service     │
│ D(1)   │ │  D(1)       │ │  D(1)        │
│ :8001  │ │  :8002      │ │  :8003       │
└───┬────┘ └──────┬──────┘ └──────────────┘
    │             │                │
    ▼             ▼                │
┌─────────┐ ┌─────────┐           │
│Postgres │ │  Redis  │           │
│ SS(1)   │ │  SS(1)  │           │
│ :5432   │ │  :6379  │           │
└─────────┘ └─────────┘           │
                                   ▼
                          ┌──────────────────┐
                          │  OpenClaw        │
                          │  Deployment + Svc│
                          │  ClusterIP:18789 │
                          └──────────────────┘

D = Deployment, SS = StatefulSet
```

### 3.2 工作负载清单

| 名称 | 类型 | 副本数 | 端口 | 原因 |
|------|------|--------|------|------|
| api-gateway | Deployment | 1 | 8000 | 无状态网关，可水平扩展 |
| case-service | Deployment | 1 | 8001 | 无状态服务 |
| conversation-service | Deployment | 1 | 8002 | 无状态服务 |
| scheduler-service | Deployment | 1 | 8003 | 无状态服务 |
| customer-ui | Deployment | 1 | 80 | 静态文件 Nginx |
| admin-ui | Deployment | 1 | 80 | 静态文件 Nginx |
| postgres | StatefulSet | 1 | 5432 | 有状态存储，需持久卷 |
| redis | StatefulSet | 1 | 6379 | 有状态缓存，需持久卷 |

**副本数为 1 的原因：** 单节点 K3s，8 CPU / 7.6 GB RAM 资源受限。当资源充足时，无状态服务可简单调整 `replicaCount` 实现水平扩展。

### 3.3 OpenClaw 接入策略

OpenClaw **已容器化**，以 Kubernetes Deployment 运行于 `hci-troubleshoot` 命名空间，镜像 `hci-openclaw:latest`，服务端口 18789。

```yaml
# OpenClaw 当前部署方式：Deployment + ClusterIP Service
# 由 Helm Chart 统一管理（deploy/helm/hci-platform/templates/ 下）
apiVersion: v1
kind: Service
metadata:
  name: openclaw
  namespace: hci-troubleshoot
spec:
  type: ClusterIP
  selector:
    app.kubernetes.io/name: openclaw
  ports:
    - port: 18789
      targetPort: 18789
```

**实际部署说明：**
- OpenClaw Gateway 已通过 `hci-openclaw` Docker 镜像容器化，纳入 Helm Chart 统一管理
- Kubernetes Deployment 方式相比 systemd + ExternalName 具备更好的滚动更新、资源限制和自愈能力
- 端口已从文档早期的 18790 统一为 18789（与 Helm values.yaml 一致）

> ⚠️ **文档历史说明**：本节早期版本描述 OpenClaw 以 systemd 运行于宿主机并使用 ExternalName Service，该方案已被 Deployment 方案替代。

---

## 4. 配置管理

### 4.1 配置分层策略

```
┌───────────────────────────────────┐
│  Helm values.yaml                 │  ← 部署参数（镜像版本、副本数、资源限制）
├───────────────────────────────────┤
│  ConfigMap (非敏感配置)            │  ← 服务 URL、日志级别、OTel 端点
├───────────────────────────────────┤
│  Secret (敏感配置)                 │  ← 数据库密码、Token、API Key
└───────────────────────────────────┘
```

### 4.2 ConfigMap 设计

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: hci-common-config
  namespace: hci-troubleshoot
data:
  # 数据库（非敏感部分）
  POSTGRES_DB: "hci_troubleshoot"
  POSTGRES_USER: "hci_admin"
  
  # Redis
  REDIS_URL: "redis://redis:6379/0"
  
  # 服务间通信（K8s 内部 DNS）
  CASE_SERVICE_URL: "http://case-service:8001"
  CONVERSATION_SERVICE_URL: "http://conversation-service:8002"
  SCHEDULER_SERVICE_URL: "http://scheduler-service:8003"
  
  # OpenClaw
  OPENCLAW_BASE_URL: "http://openclaw:18789"
  
  # 可观测性
  OTEL_EXPORTER_OTLP_ENDPOINT: "http://tempo.hci-observability.svc.cluster.local:4317"
  LOG_LEVEL: "INFO"
  
  # Scheduler
  K8S_NAMESPACE: "hci-troubleshoot"
  WARM_POOL_SIZE: "2"
  MAX_POOL_SIZE: "10"
  POD_IDLE_TIMEOUT: "300"
```

### 4.3 Secret 设计

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: hci-secrets
  namespace: hci-troubleshoot
type: Opaque
stringData:
  POSTGRES_PASSWORD: "dev_password_123"          # 生产环境必须替换
  OPENCLAW_GATEWAY_TOKEN: "hci-dev-openclaw-token"
  ZAI_API_KEY: ""                                 # 按需填写
  GF_SECURITY_ADMIN_PASSWORD: "admin"            # Grafana 管理员密码
```

**设计原因：**
- ConfigMap 与 Secret 分离是 K8s 安全最佳实践
- Secret 支持 `kubectl create secret` 动态注入，不需提交到 Git
- 所有服务通过 `envFrom` 引用同一个 ConfigMap + Secret，变量名与 `.env.example` 保持一致
- 修改配置后，Deployment 通过 annotation checksum 自动触发滚动更新

### 4.4 配置注入方式

```yaml
# Deployment 模板中的配置注入
spec:
  template:
    metadata:
      annotations:
        # 配置变更自动触发滚动更新
        checksum/config: {{ include (print $.Template.BasePath "/configmap.yaml") . | sha256sum }}
        checksum/secret: {{ include (print $.Template.BasePath "/secret.yaml") . | sha256sum }}
    spec:
      containers:
        - name: {{ .name }}
          envFrom:
            - configMapRef:
                name: hci-common-config
            - secretRef:
                name: hci-secrets
          env:
            # 组合 DATABASE_URL（引用 ConfigMap + Secret 中的值）
            - name: DATABASE_URL
              value: "postgresql+asyncpg://$(POSTGRES_USER):$(POSTGRES_PASSWORD)@postgres:5432/$(POSTGRES_DB)"
```

---

## 5. 存储设计

### 5.1 PersistentVolumeClaim

| 组件 | 存储类 | 容量 | 访问模式 | 用途 |
|------|--------|------|----------|------|
| postgres | local-path | 5Gi | ReadWriteOnce | 数据库持久存储 |
| redis | local-path | 1Gi | ReadWriteOnce | AOF 持久化 |
| loki | local-path | 2Gi | ReadWriteOnce | 日志存储 |
| tempo | local-path | 2Gi | ReadWriteOnce | 链路追踪存储 |

**设计原因：**
- K3s 内置 `local-path-provisioner`，PVC 会自动创建 PV 到 `/opt/local-path-provisioner/`
- 单节点场景下 `local-path` 足够；多节点场景需切换到 NFS 或 Longhorn
- 容量参考：当前 PostgreSQL 数据 ~50 MB，预留 5Gi 足够 MVP 阶段

### 5.2 StatefulSet 存储模板

```yaml
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: postgres
spec:
  serviceName: postgres
  replicas: 1
  volumeClaimTemplates:
    - metadata:
        name: postgres-data
      spec:
        accessModes: ["ReadWriteOnce"]
        storageClassName: local-path
        resources:
          requests:
            storage: 5Gi
  template:
    spec:
      containers:
        - name: postgres
          image: postgres:15
          ports:
            - containerPort: 5432
          volumeMounts:
            - name: postgres-data
              mountPath: /var/lib/postgresql/data
            - name: init-sql
              mountPath: /docker-entrypoint-initdb.d
          envFrom:
            - configMapRef:
                name: hci-common-config
            - secretRef:
                name: hci-secrets
      volumes:
        - name: init-sql
          configMap:
            name: postgres-init-sql
```

---

## 6. 网络与 Ingress 设计

### 6.1 Service 拓扑

| Service 名 | 类型 | 端口 | 目标端口 | 说明 |
|------------|------|------|---------|------|
| api-gateway | ClusterIP | 8000 | 8000 | 后端 API 入口 |
| case-service | ClusterIP | 8001 | 8001 | 内部调用 |
| conversation-service | ClusterIP | 8002 | 8002 | 内部调用 |
| scheduler-service | ClusterIP | 8003 | 8003 | 内部调用 |
| customer-ui | ClusterIP | 80 | 80 | 前端 C 端 |
| admin-ui | ClusterIP | 80 | 80 | 前端管理端 |
| postgres | ClusterIP (Headless) | 5432 | 5432 | StatefulSet 无头服务 |
| redis | ClusterIP (Headless) | 6379 | 6379 | StatefulSet 无头服务 |
| openclaw | ClusterIP | 18789 | 18789 | 集群内 OpenClaw 服务 |

### 6.2 Ingress 规则

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: hci-ingress
  namespace: hci-troubleshoot
  annotations:
    # Traefik 特有注解
    traefik.ingress.kubernetes.io/router.entrypoints: web
spec:
  rules:
    - http:
        paths:
          # 后端 API（优先匹配更具体的路径）
          - path: /api
            pathType: Prefix
            backend:
              service:
                name: api-gateway
                port:
                  number: 8000
          - path: /ws
            pathType: Prefix
            backend:
              service:
                name: api-gateway
                port:
                  number: 8000
          # 管理端前端
          - path: /admin
            pathType: Prefix
            backend:
              service:
                name: admin-ui
                port:
                  number: 80
          # 客户端前端（默认路由）
          - path: /
            pathType: Prefix
            backend:
              service:
                name: customer-ui
                port:
                  number: 80
```

**设计原因：**
- 使用 K3s 内置 Traefik 作为 Ingress Controller，不需额外组件
- 统一入口 `:80`，通过路径前缀分流
- `/api` + `/ws` 路由到 api-gateway，支持 REST 和 WebSocket
- `/admin` 路由到管理端，`/` 默认路由到客户端
- 后续可添加 TLS termination（Let's Encrypt via cert-manager）

### 6.3 WebSocket 支持

Traefik 原生支持 WebSocket 代理，无需特殊配置。连接通过 `/ws` 路径进入 api-gateway 的 WebSocket 端点。

---

## 7. 健康探针设计

所有后端微服务已实现 `/health` 端点。

### 7.1 探针配置

```yaml
# 通用探针模板
livenessProbe:
  httpGet:
    path: /health
    port: {{ .containerPort }}
  initialDelaySeconds: 15     # FastAPI 启动需要加载 OTel SDK
  periodSeconds: 30
  timeoutSeconds: 5
  failureThreshold: 3         # 连续 3 次失败判定不健康 → 重启

readinessProbe:
  httpGet:
    path: /health
    port: {{ .containerPort }}
  initialDelaySeconds: 10
  periodSeconds: 10
  timeoutSeconds: 3
  failureThreshold: 3         # 连续 3 次失败 → 从 Service 摘除

startupProbe:
  httpGet:
    path: /health
    port: {{ .containerPort }}
  initialDelaySeconds: 5
  periodSeconds: 5
  failureThreshold: 30       # 最多等 150s 启动
```

### 7.2 各组件探针

| 组件 | Liveness | Readiness | 说明 |
|------|----------|-----------|------|
| api-gateway | GET /health | GET /health | FastAPI 标准健康端点 |
| case-service | GET /health | GET /health | 同上 |
| conversation-service | GET /health | GET /health | 同上 |
| scheduler-service | GET /health | GET /health | 同上 |
| postgres | pg_isready | pg_isready | PostgreSQL 官方检查命令 |
| redis | redis-cli ping | redis-cli ping | Redis PING/PONG |
| customer-ui / admin-ui | GET / | GET / | Nginx 返回 200 |

---

## 8. 资源限制

### 8.1 基于实测数据的资源规划

Docker 容器实测数据（空闲态）：

| 容器 | 实测内存 | 实测 CPU |
|------|---------|---------|
| api-gateway | 82 MiB | 2.80% |
| case-service | 94 MiB | 2.65% |
| conversation-service | 95 MiB | 2.69% |
| scheduler-service | 104 MiB | 2.67% |
| customer-ui (Nginx) | 8 MiB | 0% |
| admin-ui (Nginx) | 8 MiB | 0% |
| postgres | 54 MiB | 5.76% |
| redis | 9 MiB | 0.28% |
| grafana | 97 MiB | 0.59% |
| loki | 87 MiB | 0.95% |
| promtail | 41 MiB | 0.54% |
| tempo | 37 MiB | 0.15% |
| **合计** | **~716 MiB** | **~19%** |

### 8.2 K8s Resource Requests/Limits

```yaml
# 后端微服务（FastAPI）
resources:
  requests:
    cpu: "50m"
    memory: "96Mi"
  limits:
    cpu: "500m"
    memory: "256Mi"

# 前端（Nginx 静态服务）
resources:
  requests:
    cpu: "10m"
    memory: "16Mi"
  limits:
    cpu: "100m"
    memory: "64Mi"

# PostgreSQL
resources:
  requests:
    cpu: "100m"
    memory: "64Mi"
  limits:
    cpu: "1000m"
    memory: "512Mi"

# Redis
resources:
  requests:
    cpu: "50m"
    memory: "16Mi"
  limits:
    cpu: "200m"
    memory: "128Mi"
```

### 8.3 总预算估算

| 类别 | Pod 数 | Requests(CPU) | Requests(Memory) |
|------|--------|---------------|------------------|
| 后端微服务 | 4 | 200m | 384 Mi |
| 前端 | 2 | 20m | 32 Mi |
| 数据存储 | 2 | 150m | 80 Mi |
| 可观测性 | 4 | 150m | 256 Mi |
| K3s 系统 | 3 | ~100m | ~41 Mi |
| **合计** | **15** | **~620m** | **~793 Mi** |

与当前 8 CPU / 7.6 GB 环境对比：CPU 利用率 ~8%，内存利用率 ~10%，**资源充裕**。

---

## 9. 可观测性集成

### 9.1 从 Docker Compose 迁移

当前可观测性栈 (`deploy/observability/docker-compose-obs.yml`) 包含 Loki + Tempo + Promtail + Grafana。迁移到 K8s 后：

| 组件 | Docker 模式 | K8s 模式 | 变化 |
|------|------------|---------|------|
| Tempo | 独立容器 | Deployment | 通过 Service 暴露 OTLP 端口 |
| Loki | 独立容器 | Deployment + PVC | 添加持久化存储 |
| Promtail | 挂载 docker.sock | DaemonSet | 改为读取 Pod 日志目录 |
| Grafana | 独立容器 | Deployment | 数据源自动 provisioning |
| OTel Collector | 无 | DaemonSet（可选） | 集中收集/转发 OTLP 数据 |

### 9.2 日志采集变化

```
Docker 模式:   Container → Docker Logging Driver → Promtail → Loki
K8s 模式:      Pod → /var/log/pods/ → Promtail DaemonSet → Loki
```

Promtail 在 K8s 中以 DaemonSet 运行，挂载 `/var/log/pods` 采集日志。

### 9.3 ArgoCD Application 架构（hci-platform-obs）

可观测性栈由独立 ArgoCD Application `hci-platform-obs` 管理（`deploy/gitops/argo-apps/hci-platform-obs.yaml`），与业务服务 Application 解耦。

**多源模式**：同时读取应用仓库（Chart）和环境仓库（values），Grafana 密码统一由 `hci-platform-env` 提供：

```yaml
sources:
  - repoURL: hci-troubleshoot-platform  # Helm Chart
    helm:
      valueFiles: [$values/environments/dev/values.yaml]
  - repoURL: hci-platform-env           # 环境配置（含 grafanaAdminPassword）
    ref: values
```

**PVC storageClass 注意事项**：

Loki 和 Tempo 的 PVC `storageClassName` 一旦绑定**不可变**。存量安装保持 `local-path`，全新安装可改为 `local-path-retain`（Retain 策略，卸载后 PV 保留）。
升级时若 Chart 与已有 PVC 的 `storageClass` 不一致，ArgoCD sync 会报 `spec is immutable` 错误，已通过 `ignoreDifferences` + `RespectIgnoreDifferences=true` 兜底处理。

### 9.4 Trace 链路

```
Pod (FastAPI) → OTLP gRPC → Tempo Service (hci-observability namespace)
                              ↓
                          tempo.hci-observability.svc.cluster.local:4317
```

后端微服务的 `OTEL_EXPORTER_OTLP_ENDPOINT` 从 `http://tempo:4317` 改为 `http://tempo.hci-observability.svc.cluster.local:4317`（跨 namespace 通信）。

### 9.4 ghcr.io 镜像拉取认证（imagePullSecret）

CI 构建的镜像推送到私有 ghcr.io，K8s 拉取需要认证。方案设计：

| 组件 | 说明 |
|------|------|
| Secret 名称 | `ghcr-pull-secret`（`kubernetes.io/dockerconfigjson` 类型） |
| 渲染位置 | `deploy/helm/hci-platform/templates/secret.yaml`，当 `secrets.ghcrToken` 非空时自动生成 |
| 挂载方式 | `_helpers.tpl` 的 `hci.workloadPodSpecExtras` 统一注入 `imagePullSecrets`，由 `global.imagePullSecretName` 控制 |
| 配置来源 | `hci-platform-env/environments/<env>/values.yaml` 的 `secrets.ghcrToken` + `global.imagePullUser` |

**env 仓库配置示例（dev）：**
```yaml
global:
  imageRegistry: ghcr.io/tomturing/hci-troubleshoot-platform/
  imagePullSecretName: ghcr-pull-secret
  imagePullUser: tomturing

secrets:
  ghcrToken: "<GitHub PAT with read:packages scope>"
```

> ⚠️ `ghcrToken` 存储在私有 env 仓库中，禁止提交到公开仓库。

---

## 10. Helm Chart 结构

### 10.1 Chart 组织

```
deploy/helm/hci-platform/
├── Chart.yaml                    # Chart 元数据
├── values.yaml                   # 默认配置值
├── values-dev.yaml               # 开发环境覆盖
├── values-prod.yaml              # 生产环境覆盖
├── templates/
│   ├── _helpers.tpl              # 模板辅助函数
│   ├── namespace.yaml            # 命名空间
│   ├── configmap.yaml            # 统一非敏感配置
│   ├── secret.yaml               # 统一敏感配置
│   ├── ingress.yaml              # Traefik Ingress 规则
│   │
│   ├── api-gateway/
│   │   ├── deployment.yaml
│   │   └── service.yaml
│   ├── case-service/
│   │   ├── deployment.yaml
│   │   └── service.yaml
│   ├── conversation-service/
│   │   ├── deployment.yaml
│   │   └── service.yaml
│   ├── scheduler-service/
│   │   ├── deployment.yaml
│   │   └── service.yaml
│   ├── customer-ui/
│   │   ├── deployment.yaml
│   │   └── service.yaml
│   ├── admin-ui/
│   │   ├── deployment.yaml
│   │   └── service.yaml
│   │
│   ├── postgres/
│   │   ├── statefulset.yaml
│   │   ├── service.yaml          # Headless Service
│   │   └── init-configmap.yaml   # init_schema.sql
│   └── redis/
│       ├── statefulset.yaml
│       └── service.yaml          # Headless Service
└── charts/                       # 子 Chart（可选）
```

### 10.2 values.yaml 核心结构

```yaml
# 全局配置
global:
  namespace: hci-troubleshoot
  imageRegistry: ""              # 私有镜像仓库前缀（可选）
  imagePullPolicy: IfNotPresent

# 数据库
postgres:
  image: postgres:15
  storage: 5Gi
  storageClass: local-path

# 缓存
redis:
  image: redis:7
  storage: 1Gi
  storageClass: local-path

# 后端微服务
apiGateway:
  replicaCount: 1
  image:
    repository: hci-api-gateway
    tag: latest
  resources:
    requests: { cpu: "50m", memory: "96Mi" }
    limits:   { cpu: "500m", memory: "256Mi" }

caseService:
  replicaCount: 1
  image:
    repository: hci-case-service
    tag: latest
  resources:
    requests: { cpu: "50m", memory: "96Mi" }
    limits:   { cpu: "500m", memory: "256Mi" }

conversationService:
  replicaCount: 1
  image:
    repository: hci-conversation-service
    tag: latest
  resources:
    requests: { cpu: "50m", memory: "96Mi" }
    limits:   { cpu: "500m", memory: "256Mi" }

schedulerService:
  replicaCount: 1
  image:
    repository: hci-scheduler-service
    tag: latest
  resources:
    requests: { cpu: "50m", memory: "96Mi" }
    limits:   { cpu: "500m", memory: "256Mi" }

# 前端
customerUI:
  replicaCount: 1
  image:
    repository: hci-customer-ui
    tag: latest
  resources:
    requests: { cpu: "10m", memory: "16Mi" }
    limits:   { cpu: "100m", memory: "64Mi" }

adminUI:
  replicaCount: 1
  image:
    repository: hci-admin-ui
    tag: latest
  resources:
    requests: { cpu: "10m", memory: "16Mi" }
    limits:   { cpu: "100m", memory: "64Mi" }

# OpenClaw 容器化服务
openclaw:
  port: 18789

# Ingress（Traefik）
ingress:
  enabled: true
  className: traefik
  annotations: {}

# 配置（非敏感）
config:
  logLevel: INFO
  otelEndpoint: "http://tempo.hci-observability.svc.cluster.local:4317"

# 敏感配置（生产环境通过 --set 或 sealed-secrets 覆盖）
secrets:
  postgresPassword: "dev_password_123"
  openclawToken: "hci-dev-openclaw-token"
  zaiApiKey: ""
  grafanaAdminPassword: "admin"
```

---

## 11. 部署流程

### 11.1 镜像构建

```bash
# 从项目根目录构建所有镜像（重用现有 Dockerfile）
docker build -t hci-api-gateway:latest      -f backend/api-gateway/Dockerfile ./backend
docker build -t hci-case-service:latest      -f backend/case-service/Dockerfile ./backend
docker build -t hci-conversation-service:latest  -f backend/conversation-service/Dockerfile ./backend
docker build -t hci-scheduler-service:latest -f backend/scheduler-service/Dockerfile ./backend
docker build -t hci-customer-ui:latest       -f frontend/customer/Dockerfile ./frontend
docker build -t hci-admin-ui:latest          -f frontend/admin/Dockerfile ./frontend
```

### 11.2 导入镜像到 K3s

K3s 使用 containerd，不共享 Docker 的镜像存储。需要导入：

```bash
# 方法1: docker save + k3s ctr import
docker save hci-api-gateway:latest | sudo k3s ctr images import -

# 方法2: 批量导入
for img in hci-api-gateway hci-case-service hci-conversation-service \
           hci-scheduler-service hci-customer-ui hci-admin-ui; do
  docker save ${img}:latest | sudo k3s ctr images import -
done
```

### 11.3 Helm 部署命令

```bash
# 首次安装
helm install hci-platform deploy/helm/hci-platform \
  -n hci-troubleshoot --create-namespace \
  -f deploy/helm/hci-platform/values-dev.yaml

# 更新
helm upgrade hci-platform deploy/helm/hci-platform \
  -n hci-troubleshoot \
  -f deploy/helm/hci-platform/values-dev.yaml

# 卸载
helm uninstall hci-platform -n hci-troubleshoot

# 查看渲染结果（不实际部署）
helm template hci-platform deploy/helm/hci-platform \
  -f deploy/helm/hci-platform/values-dev.yaml
```

### 11.4 部署验证

```bash
# 检查所有 Pod 状态
kubectl get pods -n hci-troubleshoot -o wide

# 检查 Service
kubectl get svc -n hci-troubleshoot

# 检查 Ingress
kubectl get ingress -n hci-troubleshoot

# 端口转发测试（如果 Ingress 未就绪）
kubectl port-forward svc/api-gateway 8000:8000 -n hci-troubleshoot

# 健康检查
curl http://localhost/api/health
```

---

## 12. 实施计划

### 12.1 分阶段实施

| 阶段 | 内容 | 依赖 | 预计耗时 |
|------|------|------|---------|
| **Phase 1** | Helm Chart 骨架 + values.yaml | 本文档 | 1h |
| **Phase 2** | PostgreSQL + Redis StatefulSet | Phase 1 | 0.5h |
| **Phase 3** | 4 个后端 Deployment + Service | Phase 2 | 1h |
| **Phase 4** | 2 个前端 Deployment + Ingress | Phase 3 | 0.5h |
| **Phase 5** | ConfigMap + Secret + 配置注入 | Phase 3 | 0.5h |
| **Phase 6** | 镜像构建 + 部署验证 | Phase 4+5 | 1h |
| **Phase 7** | 可观测性 namespace 迁移 | Phase 6 | 1h |

### 12.2 回退策略

- Helm 支持 `helm rollback hci-platform [REVISION]`，一键回退
- Docker Compose 环境保持不变，随时可切回：`docker compose up -d`
- 两套环境（Docker Compose + K8s）可共存但不可同时运行（端口冲突）

### 12.3 从 Docker Compose 迁移清单

- [ ] 停止 Docker Compose 服务：`docker compose down`
- [ ] 停止可观测性栈：`cd deploy/observability && docker compose -f docker-compose-obs.yml down`
- [ ] 构建镜像并导入 K3s
- [ ] `helm install` 部署
- [ ] 验证所有 Pod Running + Ready
- [ ] 验证 Ingress 路由正确
- [ ] 验证 E2E 功能（创建工单 → AI 对话 → 关闭工单）
- [ ] 验证可观测性（Grafana 看到日志和 Trace）

---

## 13. 安全考虑

### 13.1 当前阶段（MVP/开发）

- Secret 中的敏感值使用 base64 编码（K8s 默认）
- 不暴露 NodePort，仅通过 Ingress 访问
- PostgreSQL / Redis 仅集群内可达（Headless Service）

### 13.2 后续增强（生产阶段）

| 措施 | 说明 |
|------|------|
| Sealed Secrets / External Secrets | 加密敏感配置，避免 Secret 明文存储在 Git |
| NetworkPolicy | 限制 Pod 间通信（如 Redis 仅允许后端 Pod 访问） |
| PodSecurityStandard | 限制 Pod 权限（non-root, readOnlyRootFilesystem） |
| TLS Ingress | cert-manager + Let's Encrypt 自动签发证书 |
| RBAC | 按 namespace 隔离权限 |

---

## 附录 A: 与架构设计的映射

| 架构设计中的组件 | K8s 资源 | 说明 |
|----------------|---------|------|
| 用户层 (Web Client) | customer-ui / admin-ui Deployment | Nginx 提供静态文件 |
| 网关层 (API Gateway) | api-gateway Deployment + Ingress | Traefik 代替独立 Nginx 反向代理 |
| 服务层 (Case/Conv/Scheduler) | 3 个 Deployment | 无状态，可水平扩展 |
| AI 层 (OpenClaw) | openclaw Deployment + ClusterIP Service | 纳入 Helm 统一管理 |
| 数据层 (PostgreSQL + Redis) | 2 个 StatefulSet + PVC | 持久化存储 |
| 可观测层 (Loki/Tempo/Grafana) | 独立 namespace 部署 | 与业务解耦 |

## 附录 B: 环境变量对照表

| 变量名 | Docker Compose 来源 | K8s 来源 | 值示例 |
|--------|---------------------|---------|--------|
| DATABASE_URL | docker-compose.yml env | ConfigMap + Secret 组合 | `postgresql+asyncpg://...` |
| REDIS_URL | docker-compose.yml env | ConfigMap | `redis://redis:6379/0` |
| CASE_SERVICE_URL | docker-compose.yml env | ConfigMap | `http://case-service:8001` |
| OPENCLAW_BASE_URL | docker-compose.yml env | ConfigMap | `http://openclaw:18789` |
| OPENCLAW_GATEWAY_TOKEN | docker-compose.yml env | Secret | `hci-dev-openclaw-token` |
| POSTGRES_PASSWORD | docker-compose.yml env | Secret | `dev_password_123` |
| OTEL_EXPORTER_OTLP_ENDPOINT | config.py default | ConfigMap | `http://tempo.hci-obs...:4317` |
| LOG_LEVEL | config.py default | ConfigMap | `INFO` |

## 附录 C: CI/CD 触发方式

### 自动触发

| 事件 | 触发 job |
|------|---------|
| `push` to `main` | 全量 CI（lint / tests / build-and-push / auto-deploy-dev） |
| `pull_request` to `main` | docs-governance / lint / tests |

### 手动触发（workflow_dispatch）

进入 GitHub → `hci-troubleshoot-platform` → Actions → CI → **Run workflow**，可在任意时刻手动触发完整镜像构建和 dev 环境晋级，通常用于：

- 修复 `ImagePullBackOff`（镜像不存在于 ghcr.io 时）
- PAT token 更新后重新推送镜像
- 验证 Dockerfile 修改

<!-- CI 依赖链修复：lint/frontend-build/helm-validate 不再依赖 docs-governance -->

<!-- Docker build context 修复说明：backend 服务使用 backend/ 作为 context，frontend 服务使用 frontend/ 作为 context -->

<!-- kb-service 204 response_class 修复说明 -->
