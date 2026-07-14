# GitOps 资产目录

> 本目录包含 ArgoCD Application 清单和环境仓库骨架模板。
>
> **双仓模型**：代码仓库（本仓）负责代码和 CI，环境仓库（`hci-platform-env`）存储 Helm values。
> ArgoCD 监听环境仓库实现自动同源。

## 目录结构

```
deploy/gitops/
├── argo-apps/
│   ├── local/   ← 本地 WSL dev 环境 ArgoCD Application 清单
│   │   ├── hci-platform-infra-dev.yaml    集群级资源（dev 集群一次性）
│   │   ├── hci-platform-data-dev.yaml     数据层 dev（hci-dev ns）
│   │   ├── hci-platform-obs-dev.yaml      可观测性栈 dev（hci-observability ns）
│   │   └── hci-platform-dev.yaml          业务服务 dev（hci-dev ns）
│   └── cloud/   ← 云端 staging + prod 环境 ArgoCD Application 清单
│       ├── hci-platform-infra-staging.yaml / hci-platform-infra-prod.yaml
│       ├── hci-platform-data-staging.yaml / hci-platform-data-prod.yaml
│       ├── hci-platform-obs-staging.yaml / hci-platform-obs-prod.yaml
│       └── hci-platform-staging.yaml / hci-platform-prod.yaml
├── argocd-ops/         ← ArgoCD 运维清单（watchdog 等）
├── env-repo-template/  ← 环境仓库骨架（复制到 hci-platform-env）
└── local/              ← 本地密钥（不提交，仅本地）
```

> 详细清单见 [argo-apps/README.md](argo-apps/README.md)。dev 环境用 `argo-apps/local/`，
> staging/prod 用 `argo-apps/cloud/`。

## 首次部署顺序（dev / 本地 WSL）

```bash
# 1. 集群级资源（StorageClass + ClusterRole，每个集群仅需一次）
kubectl apply -f deploy/gitops/argo-apps/local/hci-platform-infra-dev.yaml

# 2. 数据层（PostgreSQL + Redis）
kubectl apply -f deploy/gitops/argo-apps/local/hci-platform-data-dev.yaml

# 3. 可观测性栈
kubectl apply -f deploy/gitops/argo-apps/local/hci-platform-obs-dev.yaml

# 4. 业务服务
kubectl apply -f deploy/gitops/argo-apps/local/hci-platform-dev.yaml
```

## 环境仓库接入

1. 新建独立环境仓库（示例：`hci-platform-env`）
2. 将 `env-repo-template/` 下内容复制到环境仓库根目录
3. 修改 `argo-apps/local/*.yaml` 与 `argo-apps/cloud/*.yaml` 中的 `repoURL` 和 `targetRevision`
4. 配置 ArgoCD 仓库凭据（见 [argo-apps/local/README.md](argo-apps/local/README.md)）
